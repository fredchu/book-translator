#!/usr/bin/env python3
"""End-to-end book translation driver using a local Ollama or omlx model.

Sequential per-chapter loop (single GPU). Phase 1 marker alignment is enforced
on every chunk; misalignment triggers one retry with a higher temperature,
then recursive split-on-fail down to a single-paragraph minimal fallback.
State.json drives resume; re-running on the same --out picks up where the
previous run stopped.

Pipeline:
    extract_epub.extract → init_state → for each translate spine item:
        build prompt with marker + carryover + minimal glossary
        OllamaProvider.translate (attempt 0)
        strip_known_leak_prefixes + extract_aligned_translation
        on misalign: retry with temperature=0.5 (attempt 1), split, fallback
        write chapters/<id>_translation.txt + translation_log/<id>.json
        update carryover (last 200 chars), save state.json
    assemble.assemble (strict_nav=False — ollama path may have nav gaps)
    audit_suite.run_all (4 deterministic gates; --no-audit to skip)

Example:
    python3 scripts/translate_book_ollama.py \\
        --book "/path/to/book.epub" \\
        --ollama-model hy-mt2:7b \\
        --out /tmp/translations/

Produces:
    /tmp/translations/<book_stem>/        — extract artifacts + state.json
    /tmp/translations/<book_stem>/chapters/<id>_translation.txt — per chapter
    /tmp/translations/<book_stem>/translation_log/<id>.json — replay log
    /tmp/translations/<book_stem>_bilingual.epub — final bilingual EPUB
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import chunker  # noqa: E402
import dispatch  # noqa: E402
import extract_epub  # noqa: E402
import marker_alignment as ma  # noqa: E402
import offline_postprocess  # noqa: E402
import state as state_mod  # noqa: E402
import translation_log  # noqa: E402
from assemble import assemble  # noqa: E402
from audit_suite import all_passed, format_summary, run_all as run_audits  # noqa: E402
from providers import OllamaProvider, OmlxProvider, ProviderError  # noqa: E402


MINIMAL_GLOSSARY = {
    "characters": {},
    "places": {},
    "terms": {},
    "chapter_titles_zh": {},
    "style_anchor": {
        "register": "non_fiction_narrative",
        "avoid": ["翻譯腔", "過度書面化", "簡體中文用語"],
        "prefer": ["短句", "口語節奏", "台灣繁體中文"],
    },
}


LocalSequentialProvider = OllamaProvider | OmlxProvider


class BookPathAction(argparse.Action):
    """Collect one or more --book values, including repeated --book flags."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: list[Path],
        option_string: str | None = None,
    ) -> None:
        current = getattr(namespace, self.dest, None) or []
        current.extend(values)
        setattr(namespace, self.dest, current)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--book",
        required=True,
        nargs="+",
        type=Path,
        action=BookPathAction,
        help="path(s) to .epub",
    )
    parser.add_argument("--engine", choices=["ollama", "omlx"], default="omlx",
                        help="default omlx (Qwopus3.6-27B-v2-MLX-4bit) — quality-first offline path; Qwen3.6-35B-Heretic-4bit is the ~4x faster alternate")
    parser.add_argument("--ollama-model", default=None, help="e.g. hy-mt2:7b / translategemma:27b")
    parser.add_argument("--omlx-model", default="Qwopus3.6-27B-v2-MLX-4bit",
                        help="default Qwopus3.6-27B-v2-MLX-4bit (Opus-distilled dense 27B; better prose rhythm and 台灣 usage on human read-through, ~0.84 min/chunk). Use Qwen3.6-35B-Heretic-4bit for ~4x speed when draft quality suffices")
    parser.add_argument("--out", required=False, type=Path, default=None,
                        help="output parent dir; per-book dir created inside (default: book's parent dir)")
    parser.add_argument("--ollama-host", default="http://localhost:11434")
    parser.add_argument("--omlx-host", default="http://localhost:8090")
    parser.add_argument("--omlx-api-key", default=os.environ.get("OMLX_API_KEY"),
                        help="Bearer token for the omlx-compatible endpoint (cloud vLLM via cloud_llm.sh sets OMLX_API_KEY); local omlx needs none")
    parser.add_argument("--book-title", default=None, help="title injected into prompt (default: book stem)")
    parser.add_argument("--target-lang", default="zh-tw")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--num-ctx", type=int, default=8192,
                        help="num_ctx per chunk; smaller is faster (default 8192 — chunks ≤ ~3K source chars)")
    parser.add_argument("--num-predict", type=int, default=4096,
                        help="num_predict per chunk; output is 60-80%% of input tokens")
    parser.add_argument("--temperature", type=float, default=0.3)
    parser.add_argument("--chunk-max-chars", type=int, default=3000,
                        help="source-side chunk budget in chars (Bocky default ~1500 tokens ≈ 3000 chars)")
    parser.add_argument("--max-concurrent-requests", type=int, default=1,
                        help="omlx engine only; >1 targets a cloud vLLM/SGLang endpoint doing continuous "
                             "batching and dispatches a chapter's chunks concurrently. Local single-GPU "
                             "omlx should stay at the default (1 = sequential, unchanged behavior)")
    parser.add_argument("--no-seam-repair", action="store_true",
                         help="skip the post-hoc seam-repair pass (only relevant when "
                              "--max-concurrent-requests > 1); repair re-translates each chunk "
                              "boundary paragraph with its real preceding translation as context")
    parser.add_argument("--no-audit", action="store_true", help="skip the 4 deterministic audits at the end")
    parser.add_argument("--no-resume", action="store_true", help="re-extract + re-translate from scratch")
    parser.add_argument("--limit", type=int, default=None, help="cap on chapters translated this run (debug)")
    return parser


def _translate_chunk(
    provider: LocalSequentialProvider,
    *,
    chunk_paragraphs: tuple[str, ...],
    chapter_label: str,
    chunk_label: str,
    book_title: str,
    target_lang: str,
    carryover: str,
    book_dir: Path,
    default_temperature: float,
) -> tuple[str | None, str, list[str]]:
    """Translate one chunk (≤ ~3000 source chars). Returns (aligned, raw, warnings).

    Two attempts: attempt 0 at default temperature, attempt 1 at temperature=0.5.
    Marker contract is local within the chunk: prompt shows [[PARA_1]]..[[PARA_N]]
    where N == len(chunk_paragraphs). Caller is responsible for stitching.
    """
    # Compact Ollama prompt: system = stable role + marker contract,
    # user = per-chunk paragraphs + optional carryover. Removes glossary JSON,
    # style anchor, register rules, and self-verification block that confused
    # hy-mt2:7b into empty-stop on item_004/item_005 of The Next Renaissance.
    system_msg, user_msg = dispatch.build_ollama_chunk_prompt(
        chunk_paragraphs=chunk_paragraphs,
        target_lang=target_lang,
        carryover=carryover,
        fixed_terms=dispatch.load_fixed_terms(book_dir),
    )
    expected_count = len(chunk_paragraphs)
    log_dir = book_dir / "_ollama_logs"
    raw = ""
    warnings: list[str] = []
    # Build a synthetic chunk_html only to feed dispatch.validate_translation
    # (which expects raw HTML for paragraph counting); the actual prompt sent
    # to ollama is system_msg + user_msg above.
    chunk_html_for_validate = "".join(f"<p>{p}</p>" for p in chunk_paragraphs)

    for attempt in (0, 1):
        attempt_temperature = 0.5 if attempt == 1 else default_temperature
        try:
            result = provider.translate(
                user_msg,
                request_id=f"{chapter_label}_{chunk_label}_attempt_{attempt}",
                log_dir=log_dir,
                system=system_msg,
                temperature=attempt_temperature,
            )
        except ProviderError as exc:
            warnings.append(f"attempt {attempt}: ProviderError: {exc}")
            continue
        raw = result.raw_text
        attempt_warnings = dispatch.validate_translation(raw, chunk_html_for_validate)
        warnings.extend(f"attempt {attempt}: {w}" for w in attempt_warnings)
        cleaned = dispatch.strip_known_leak_prefixes(raw)
        try:
            aligned = dispatch.extract_aligned_translation(cleaned, expected_count=expected_count)
            return aligned, raw, warnings
        except ValueError as exc:
            warnings.append(f"attempt {attempt}: {exc}")
            continue

    return None, raw, warnings


def _translate_single_paragraph_fallback(
    provider: LocalSequentialProvider,
    paragraph: str,
    target_lang: str,
    book_dir: Path,
    depth_label: str,
    *,
    temperature: float | None = None,
) -> tuple[str | None, list[str]]:
    """Translate one paragraph with the minimal no-marker fallback prompt."""
    system_msg, user_msg = dispatch.build_minimal_paragraph_prompt(
        paragraph=paragraph,
        target_lang=target_lang,
    )
    log_dir = book_dir / "_ollama_logs"
    warnings: list[str] = []
    try:
        result = provider.translate(
            user_msg,
            request_id=f"{depth_label}_fallback",
            log_dir=log_dir,
            system=system_msg,
            temperature=temperature,
        )
    except ProviderError as exc:
        return None, [f"{depth_label}: fallback ProviderError: {exc}"]

    raw = result.raw_text or ""
    cleaned = dispatch.strip_known_leak_prefixes(raw).strip()
    if not cleaned:
        warnings.append(f"{depth_label}: fallback empty response")
        return None, warnings
    # This path never goes through the marker parser (it's the no-marker
    # floor), so an internal blank line here isn't caught by
    # parse_marker_output's collapsing — do it directly, same reason: a
    # blank line inside what's meant to be ONE paragraph is indistinguishable
    # from a real paragraph boundary once chunker.stitch() joins pieces.
    cleaned, had_blank_line = ma.collapse_internal_blank_lines(cleaned)
    if had_blank_line:
        warnings.append(
            f"{depth_label}: fallback blank line inside paragraph, collapsed"
        )
    refusal = dispatch.detect_aup_refusal(cleaned)
    if refusal:
        warnings.append(f"{depth_label}: fallback {refusal}")
        return None, warnings
    return cleaned, warnings


def _translate_chunk_with_recursion(
    provider: LocalSequentialProvider,
    *,
    chunk_paragraphs: tuple[str, ...],
    chapter_label: str,
    chunk_label: str,
    book_title: str,
    target_lang: str,
    carryover: str,
    book_dir: Path,
    default_temperature: float,
    depth: int = 0,
    max_depth: int = 4,
) -> tuple[str | None, list[str]]:
    """Translate a chunk, splitting failed chunks until fallback floor."""
    aligned, _raw, warns = _translate_chunk(
        provider,
        chunk_paragraphs=chunk_paragraphs,
        chapter_label=chapter_label,
        chunk_label=chunk_label,
        book_title=book_title,
        target_lang=target_lang,
        carryover=carryover,
        book_dir=book_dir,
        default_temperature=default_temperature,
    )
    all_warnings = [f"{chunk_label}: {w}" for w in warns]
    if aligned is not None:
        return aligned, all_warnings

    if len(chunk_paragraphs) == 1:
        print(f"[split] {chapter_label}_{chunk_label} fallback single paragraph", file=sys.stderr)
        # temperature=0.5: this branch is only reached after both marker
        # attempts in _translate_chunk failed, the second of which used 0.5 —
        # keep that same higher-temperature hedge for the last-resort fallback
        # (this used to happen implicitly via the now-removed provider.temperature
        # mutation; making it explicit preserves the same behavior).
        fallback, fallback_warnings = _translate_single_paragraph_fallback(
            provider,
            chunk_paragraphs[0],
            target_lang,
            book_dir,
            chunk_label,
            temperature=0.5,
        )
        all_warnings.extend(fallback_warnings)
        return fallback, all_warnings

    if depth >= max_depth:
        print(
            f"[split] {chapter_label}_{chunk_label} max_depth={max_depth}; "
            f"fallback {len(chunk_paragraphs)} paragraph(s)",
            file=sys.stderr,
        )
        fallback_parts: list[str] = []
        for idx, paragraph in enumerate(chunk_paragraphs, start=1):
            paragraph_label = f"{chunk_label}-P{idx:02d}"
            fallback, fallback_warnings = _translate_single_paragraph_fallback(
                provider,
                paragraph,
                target_lang,
                book_dir,
                paragraph_label,
                temperature=0.5,
            )
            all_warnings.extend(fallback_warnings)
            if fallback is None:
                all_warnings.append(f"{paragraph_label}: fallback failed at max_depth")
                return None, all_warnings
            fallback_parts.append(fallback)
        return chunker.stitch(fallback_parts), all_warnings

    mid = len(chunk_paragraphs) // 2
    left = chunk_paragraphs[:mid]
    right = chunk_paragraphs[mid:]
    left_label = f"{chunk_label}-L"
    right_label = f"{chunk_label}-R"
    print(
        f"[split] {chapter_label}_{chunk_label} failed; split "
        f"{len(chunk_paragraphs)} -> {len(left)} + {len(right)}",
        file=sys.stderr,
    )
    left_aligned, left_warnings = _translate_chunk_with_recursion(
        provider,
        chunk_paragraphs=left,
        chapter_label=chapter_label,
        chunk_label=left_label,
        book_title=book_title,
        target_lang=target_lang,
        carryover=carryover,
        book_dir=book_dir,
        default_temperature=default_temperature,
        depth=depth + 1,
        max_depth=max_depth,
    )
    all_warnings.extend(left_warnings)
    right_carryover = left_aligned[-200:] if left_aligned else carryover
    right_aligned, right_warnings = _translate_chunk_with_recursion(
        provider,
        chunk_paragraphs=right,
        chapter_label=chapter_label,
        chunk_label=right_label,
        book_title=book_title,
        target_lang=target_lang,
        carryover=right_carryover,
        book_dir=book_dir,
        default_temperature=default_temperature,
        depth=depth + 1,
        max_depth=max_depth,
    )
    all_warnings.extend(right_warnings)
    if left_aligned is None or right_aligned is None:
        all_warnings.append(f"{chunk_label}: recursive split failed")
        return None, all_warnings
    return chunker.stitch([left_aligned, right_aligned]), all_warnings


def _translate_chapter_chunked(
    provider: LocalSequentialProvider,
    *,
    html: str,
    chapter_id: str,
    chapter_label: str,
    book_title: str,
    target_lang: str,
    carryover: str,
    book_dir: Path,
    default_temperature: float,
    chunk_max_chars: int,
) -> tuple[str | None, list[str], int]:
    """Translate a chapter via paragraph chunking.

    Returns (stitched_or_None, warnings, partial_paragraph_count).
    """
    paragraphs = dispatch.html_to_paragraphs(html)
    if not paragraphs:
        return "", [], 0
    plan = chunker.chunk_paragraphs(paragraphs, max_chars=chunk_max_chars)

    accumulated: list[str] = []
    chunk_carry = carryover
    all_warnings: list[str] = []
    partial_paragraph_count = 0

    for i, chunk in enumerate(plan.chunks, start=1):
        chunk_label = f"ck{i:02d}of{len(plan.chunks):02d}"
        aligned, warns = _translate_chunk_with_recursion(
            provider,
            chunk_paragraphs=chunk.paragraphs,
            chapter_label=chapter_label,
            chunk_label=chunk_label,
            book_title=book_title,
            target_lang=target_lang,
            carryover=chunk_carry,
            book_dir=book_dir,
            default_temperature=default_temperature,
        )
        all_warnings.extend(
            [f"{chunk_label} (paras {chunk.start_idx + 1}-{chunk.end_idx}): {w}" for w in warns]
        )
        if aligned is None:
            partial_paragraph_count += len(chunk.paragraphs)
            all_warnings.append(
                f"{chunk_label}: source-preserved {len(chunk.paragraphs)} paragraph(s) after recursive failure"
            )
            aligned = "\n\n".join(f"[未譯：模型拒答] {p}" for p in chunk.paragraphs)
        accumulated.append(aligned)
        chunk_carry = aligned[-200:]

    all_warnings.append(f"partial_paragraph_count={partial_paragraph_count}")
    return chunker.stitch(accumulated), all_warnings, partial_paragraph_count


def _intra_chapter_carries(chunks: list[chunker.Chunk], initial: str) -> list[str]:
    """Per-chunk carryover context, computed entirely from SOURCE text.

    Chunk 0 gets `initial` (the incoming cross-chapter carry). Chunk i>0 gets
    chunker.source_tail() of chunk i-1's own source paragraphs — never chunk
    i-1's translation. Every entry is known before any chunk is translated;
    that is what removes the chunk1->chunk2->...->chunkN wait chain.
    """
    carries = [initial]
    for prev in chunks[:-1]:
        carries.append(chunker.source_tail(prev.paragraphs))
    return carries


def _resolve_max_workers(provider: LocalSequentialProvider) -> int:
    """Concurrent batch width for one chapter's chunks.

    Defaults to 1 (not the chapter's chunk count) when the provider doesn't
    declare `max_concurrent_requests` — a provider with
    `supports_concurrency=True` but no explicit limit (e.g. AnthropicProvider)
    should not silently fan out to an unconfigured, unbounded level.
    """
    return max(1, getattr(provider, "max_concurrent_requests", 1))


def _translate_chunks_concurrently(
    provider: LocalSequentialProvider,
    jobs: list[dict],
    max_workers: int,
) -> list[tuple[str | None, list[str]]]:
    """Run _translate_chunk_with_recursion for each job in a thread pool.

    Callers must ensure jobs are independent — no job's kwargs may depend on
    another job's result (see _intra_chapter_carries). The HTTP calls inside
    are I/O-bound, so a thread pool gets real concurrency despite the GIL;
    the actual request batching happens server-side (vLLM/SGLang continuous
    batching) — this just needs more than one request in flight at once.

    `ThreadPoolExecutor.map()` yields results in job order regardless of
    which thread finishes first (index-slot reassembly, not arrival order) —
    that satisfies "reassemble correctly even if responses arrive out of
    order" without any manual bookkeeping here.
    """
    if not jobs:
        return []

    def _run(job: dict) -> tuple[str | None, list[str]]:
        return _translate_chunk_with_recursion(provider, **job)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        return list(pool.map(_run, jobs))


def _repair_chunk_seams(
    provider: LocalSequentialProvider,
    *,
    plan: "chunker.ChunkPlan",
    chunk_texts: list[str],
    chapter_label: str,
    book_title: str,
    target_lang: str,
    book_dir: Path,
    default_temperature: float,
    max_workers: int,
) -> tuple[list[str], int, list[str]]:
    """Re-translate each chunk boundary's first paragraph with the REAL
    translated tail of the previous chunk (chunk_texts, pass-1 output) as
    context. Pass 1 only had that chunk's own source text to stay
    parallel-safe — this pass gets that real context back for the one
    paragraph on each side of a boundary. Named after research-concurrency.md
    §2 strategy 4 ("post-hoc seam repair"); that section is unverified (only
    §1 has been fact-checked, and failed — see the file's 2026-09-09
    annotation), so treat the strategy name as a label, not a cited result.
    Every boundary depends only on its own immediate predecessor's
    already-finished pass-1 text, so all boundaries are independent of each
    other and this second pass is itself dispatched concurrently.

    Isolating "the first paragraph" from a chunk's stitched translation
    relies on the same `"\\n\\n".join(paragraph_translations)` convention
    used throughout this pipeline (dispatch.extract_aligned_translation,
    chunker.stitch, and the failure placeholder text) — this holds unless a
    single paragraph's own translation happens to contain a literal blank
    line, which the marker-per-line prompt contract does not ask the model
    to produce. Not defended against further; a chunk with exactly one
    paragraph needs no split (the seam translation replaces it whole).

    Returns (patched chunk_texts, paragraphs re-translated, warnings).
    """
    seam_jobs: list[dict] = []
    seam_boundary_for_job: list[int] = []
    for i in range(1, len(plan.chunks)):
        chunk = plan.chunks[i]
        if not chunk.paragraphs:
            continue
        seam_jobs.append(
            dict(
                chunk_paragraphs=(chunk.paragraphs[0],),
                chapter_label=chapter_label,
                chunk_label=f"seam{i:02d}",
                book_title=book_title,
                target_lang=target_lang,
                carryover=chunk_texts[i - 1][-200:],
                book_dir=book_dir,
                default_temperature=default_temperature,
            )
        )
        seam_boundary_for_job.append(i)

    if not seam_jobs:
        return chunk_texts, 0, []

    results = _translate_chunks_concurrently(provider, seam_jobs, max_workers)

    patched = list(chunk_texts)
    repaired_count = 0
    warnings: list[str] = []
    for i, (seam_aligned, seam_warns) in zip(seam_boundary_for_job, results):
        warnings.extend(f"seam{i:02d}: {w}" for w in seam_warns)
        if seam_aligned is None:
            warnings.append(f"seam{i:02d}: repair failed, keeping pass-1 translation")
            continue
        split = patched[i].split("\n\n", 1)
        if len(split) == 1:
            # single-paragraph chunk: the seam translation IS the whole chunk
            patched[i] = seam_aligned
        else:
            _old_first, remainder = split
            patched[i] = seam_aligned + "\n\n" + remainder
        repaired_count += 1

    return patched, repaired_count, warnings


def _translate_chapter_chunked_concurrent(
    provider: LocalSequentialProvider,
    *,
    html: str,
    chapter_id: str,
    chapter_label: str,
    book_title: str,
    target_lang: str,
    carryover: str,
    book_dir: Path,
    default_temperature: float,
    chunk_max_chars: int,
    seam_repair: bool = True,
) -> tuple[str | None, list[str], int]:
    """Concurrent counterpart of _translate_chapter_chunked.

    Only entered when provider.supports_concurrency is True (opt-in via
    --max-concurrent-requests > 1, meant for a cloud vLLM/SGLang endpoint —
    see OmlxProvider). Two differences from the sequential function, both
    needed to remove the chunk-to-chunk wait chain within one chapter:

    1. Every chunk's carryover context comes from the PREVIOUS chunk's own
       SOURCE text (_intra_chapter_carries), computed up front — not from
       the previous chunk's translation. This is what makes every chunk's
       prompt independent of every other chunk's completion, which is the
       actual lock. This is a trade paid to unlock concurrency, NOT a
       verified quality improvement: research-concurrency.md's §1 claim that
       source-side context "matches or beats" target-side was fact-checked
       against its own five cited papers (2026-09-09 annotation at the top
       of that file) and found unsupported — the papers it cites say the
       opposite where they say anything, and none address chunk/chapter
       boundaries specifically. The size of the quality cost from this
       switch is unmeasured.
    2. Chunks are submitted to the provider concurrently (bounded by
       provider.max_concurrent_requests) instead of one at a time.

    Concurrency here is within-chapter only — chapters are still processed
    one at a time by the caller (translate_single_book), in submission order,
    so there is no benefit to decoupling the cross-chapter carry from the
    previous chapter's real translation: it stays target-text-based on both
    the sequential and concurrent path (only the intra-chapter carry above
    is source-text-based — that's the one removing an actual wait chain).

    Losing the real translated tail at each boundary is repaired afterwards
    by `seam_repair` (default on; --no-seam-repair disables it): each
    boundary's first paragraph is re-translated once more with the REAL
    previous chunk's translation as context.
    """
    paragraphs = dispatch.html_to_paragraphs(html)
    if not paragraphs:
        return "", [], 0
    plan = chunker.chunk_paragraphs(paragraphs, max_chars=chunk_max_chars)
    if not plan.chunks:
        return "", [], 0

    chunk_carries = _intra_chapter_carries(plan.chunks, carryover)
    max_workers = _resolve_max_workers(provider)

    jobs = [
        dict(
            chunk_paragraphs=chunk.paragraphs,
            chapter_label=chapter_label,
            chunk_label=f"ck{i:02d}of{len(plan.chunks):02d}",
            book_title=book_title,
            target_lang=target_lang,
            carryover=chunk_carries[i - 1],
            book_dir=book_dir,
            default_temperature=default_temperature,
        )
        for i, chunk in enumerate(plan.chunks, start=1)
    ]
    pass1_results = _translate_chunks_concurrently(provider, jobs, max_workers)

    all_warnings: list[str] = []
    partial_paragraph_count = 0
    chunk_texts: list[str] = []
    for i, (chunk, (aligned, warns)) in enumerate(zip(plan.chunks, pass1_results), start=1):
        chunk_label = f"ck{i:02d}of{len(plan.chunks):02d}"
        all_warnings.extend(
            [f"{chunk_label} (paras {chunk.start_idx + 1}-{chunk.end_idx}): {w}" for w in warns]
        )
        if aligned is None:
            partial_paragraph_count += len(chunk.paragraphs)
            all_warnings.append(
                f"{chunk_label}: source-preserved {len(chunk.paragraphs)} paragraph(s) after recursive failure"
            )
            aligned = "\n\n".join(f"[未譯：模型拒答] {p}" for p in chunk.paragraphs)
        chunk_texts.append(aligned)

    seam_repair_count = 0
    if seam_repair and len(plan.chunks) > 1:
        chunk_texts, seam_repair_count, seam_warnings = _repair_chunk_seams(
            provider,
            plan=plan,
            chunk_texts=chunk_texts,
            chapter_label=chapter_label,
            book_title=book_title,
            target_lang=target_lang,
            book_dir=book_dir,
            default_temperature=default_temperature,
            max_workers=max_workers,
        )
        all_warnings.extend(seam_warnings)

    all_warnings.append(f"partial_paragraph_count={partial_paragraph_count}")
    all_warnings.append(f"seam_repair_paragraphs={seam_repair_count}/{len(paragraphs)}")
    return chunker.stitch(chunk_texts), all_warnings, partial_paragraph_count


def _record_partial_paragraphs(
    *,
    log_path: Path,
    partial_paragraph_count: int,
) -> None:
    entry = json.loads(log_path.read_text(encoding="utf-8"))
    entry["partial_paragraph_count"] = partial_paragraph_count
    log_path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")


def translate_single_book(book_path: Path, args: argparse.Namespace) -> dict[str, object]:
    started = time.monotonic()
    out_parent = args.out or book_path.parent
    out_parent.mkdir(parents=True, exist_ok=True)
    book_stem = book_path.stem
    book_dir = out_parent / book_stem
    state_path = book_dir / "state.json"
    manifest_path = book_dir / "manifest.json"

    return_code = 0
    error_summary = ""

    if args.no_resume or not manifest_path.exists():
        print(f"[extract] {book_path.name} -> {book_dir}", file=sys.stderr)
        extract_epub.extract(book_path, out_parent)
    else:
        print(f"[extract] cached at {book_dir} (--no-resume to redo)", file=sys.stderr)

    if state_path.exists() and not args.no_resume:
        state = state_mod.load(state_path)
        assert state is not None
        print(f"[state] resumed from {state_path}", file=sys.stderr)
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        state = state_mod.init_state(book_path, manifest.get("spine", []), args.target_lang)
        state_mod.save(state_path, state)
        print(f"[state] initialized {len(state['chapters'])} chapters at {state_path}", file=sys.stderr)

    if args.engine == "ollama":
        selected_model = args.ollama_model
        selected_host = args.ollama_host
        provider = OllamaProvider(
            model=args.ollama_model,
            host=args.ollama_host,
            timeout=args.timeout,
            num_ctx=args.num_ctx,
            num_predict=args.num_predict,
            temperature=args.temperature,
        )
    else:
        selected_model = args.omlx_model
        selected_host = args.omlx_host
        provider = OmlxProvider(
            model=args.omlx_model,
            host=args.omlx_host,
            timeout=args.timeout,
            max_tokens=args.num_predict,
            temperature=args.temperature,
            max_concurrent_requests=args.max_concurrent_requests,
            **({"api_key": args.omlx_api_key} if args.omlx_api_key else {}),
        )
    if not provider.ping():
        error_summary = f"{args.engine} unreachable at {selected_host}"
        print(f"ERROR: {error_summary}", file=sys.stderr)
        return_code = 2
        duration_sec = time.monotonic() - started
        return {
            "path": book_path,
            "status": "failed",
            "duration_sec": duration_sec,
            "error_summary": error_summary,
            "return_code": return_code,
        }

    book_title = args.book_title or book_stem
    chapters = state["chapters"]
    translate_ids = sorted(
        cid for cid, entry in chapters.items() if entry.get("output_strategy") == state_mod.TRANSLATE
    )
    print(
        f"[run] engine={args.engine} model={selected_model} chapters={len(translate_ids)} "
        f"target={args.target_lang}",
        file=sys.stderr,
    )

    # Chapters are translated one at a time regardless of provider concurrency
    # (only a chapter's OWN chunks are dispatched concurrently — see
    # _translate_chapter_chunked_concurrent) — so removing the cross-chapter
    # data dependency here would buy nothing (chapters never run out of
    # order) while losing the real translated tail at all 19 chapter
    # boundaries. Cross-chapter carryover stays target-text-based on BOTH
    # paths; only the intra-chapter carry (the actual concurrency win) is
    # source-text-based.
    use_concurrent = getattr(provider, "supports_concurrency", False)

    carryover = ""
    done_count = 0
    failed_count = 0
    skipped_count = 0
    translate_started = time.monotonic()

    for i, cid in enumerate(translate_ids, 1):
        if args.limit is not None and (done_count + failed_count) >= args.limit:
            print(f"[run] --limit={args.limit} reached; stopping", file=sys.stderr)
            break
        entry = chapters[cid]
        chapters_dir = book_dir / "chapters"
        translation_path = chapters_dir / f"{cid}_translation.txt"
        if entry.get("status") == state_mod.DONE and translation_path.exists() and not args.no_resume:
            skipped_count += 1
            carryover = translation_path.read_text(encoding="utf-8")[-200:]
            continue

        html_path = chapters_dir / f"{cid}.html"
        if not html_path.exists():
            print(f"[{i}/{len(translate_ids)}] {cid} ERR html missing", file=sys.stderr)
            state_mod.mark_failed(state, cid, "extracted html missing")
            failed_count += 1
            state_mod.save(state_path, state)
            continue
        html = html_path.read_text(encoding="utf-8")
        ch_label = cid.replace("item_", "").lstrip("0") or "0"

        t0 = time.monotonic()
        source_paragraphs = dispatch.html_to_paragraphs(html)
        total_paragraphs = len(source_paragraphs)
        if use_concurrent:
            aligned, warns, partial_paragraph_count = _translate_chapter_chunked_concurrent(
                provider,
                html=html,
                chapter_id=cid,
                chapter_label=ch_label,
                book_title=book_title,
                target_lang=args.target_lang,
                carryover=carryover,
                book_dir=book_dir,
                default_temperature=args.temperature,
                chunk_max_chars=args.chunk_max_chars,
                seam_repair=not args.no_seam_repair,
            )
        else:
            aligned, warns, partial_paragraph_count = _translate_chapter_chunked(
                provider,
                html=html,
                chapter_id=cid,
                chapter_label=ch_label,
                book_title=book_title,
                target_lang=args.target_lang,
                carryover=carryover,
                book_dir=book_dir,
                default_temperature=args.temperature,
                chunk_max_chars=args.chunk_max_chars,
            )
        elapsed = time.monotonic() - t0

        log_path = translation_log.write_log_entry(
            book_dir=book_dir,
            chapter_id=cid,
            prompt=f"(see _ollama_logs/{ch_label}_ck*.json)",
            raw_response="(chunked — see _ollama_logs for raw chunks)",
            parsed_translation=aligned or "",
            validation_warnings=warns,
            model=selected_model,
            source_paragraph_count=total_paragraphs,
        )
        _record_partial_paragraphs(log_path=log_path, partial_paragraph_count=partial_paragraph_count)

        if aligned is not None and (total_paragraphs == 0 or partial_paragraph_count < total_paragraphs):
            # Convert any residual Simplified leak to Taiwan Traditional before persisting.
            aligned = offline_postprocess.to_traditional(aligned)
            translation_path.write_text(aligned, encoding="utf-8")
            state_mod.mark_done(state, cid, aligned)
            if partial_paragraph_count > 0:
                state["chapters"][cid]["partial_paragraphs"] = partial_paragraph_count
            done_count += 1
            carryover = aligned[-200:]
            status_label = "done_partial" if partial_paragraph_count > 0 else "done"
            print(
                f"[{i}/{len(translate_ids)}] {cid} {status_label} {elapsed:.1f}s "
                f"chars={len(aligned)} partial_paragraphs={partial_paragraph_count}",
                file=sys.stderr,
            )
        else:
            state_mod.mark_failed(state, cid, "all paragraphs failed after recursive fallback")
            failed_count += 1
            print(
                f"[{i}/{len(translate_ids)}] {cid} FAILED {elapsed:.1f}s warns={warns[:2]}",
                file=sys.stderr,
            )
        state_mod.save(state_path, state)

    total = time.monotonic() - translate_started
    print(
        f"[translate] done={done_count} failed={failed_count} skipped={skipped_count} "
        f"elapsed={total/60:.1f}min",
        file=sys.stderr,
    )

    if failed_count > 0:
        print(f"[warn] {failed_count} chapters failed; bilingual epub may be incomplete", file=sys.stderr)

    not_finished = sum(
        1
        for cid in translate_ids
        if chapters[cid].get("status") not in (state_mod.DONE, state_mod.AUP_REFUSED)
    )
    if not_finished > 0:
        print(
            f"[skip-assemble] {not_finished} chapters still pending/failed; "
            f"rerun (resume) to finish before assembling",
            file=sys.stderr,
        )
        duration_sec = time.monotonic() - started
        return {
            "path": book_path,
            "status": "success",
            "duration_sec": duration_sec,
            "error_summary": "",
            "return_code": 0,
        }

    # Offline coherence passes before assembly (this path has no glossary):
    # (a) merge minority character-name transliteration variants into the
    #     dominant form, (b) populate bilingual ToC nav labels from chapter titles.
    name_merges = offline_postprocess.normalize_character_names(book_dir)
    if name_merges:
        preview = ", ".join(f"{v}->{c}" for v, c, _ in name_merges[:8])
        print(f"[postprocess] normalized {len(name_merges)} name variant(s): {preview}", file=sys.stderr)
    # The 中譯（English）rule fires per chunk, so terms get re-glossed in every
    # chunk that mentions them; keep only the first mention book-wide.
    gloss_dupes = offline_postprocess.dedupe_inline_glosses(book_dir)
    if gloss_dupes:
        preview = ", ".join(f"{t} x{n}" for t, n in gloss_dupes[:8])
        print(f"[postprocess] removed {sum(n for _, n in gloss_dupes)} repeat "
              f"gloss(es) across {len(gloss_dupes)} term(s): {preview}", file=sys.stderr)
    # Spec §5.2: after the first 中譯（AI）, later 人工智慧 become bare AI.
    # Must run after dedupe, which leaves exactly one gloss per term to learn from.
    collapsed = offline_postprocess.collapse_acronym_glosses(book_dir)
    if collapsed:
        preview = ", ".join(f"{zh}→{a} x{n}" for zh, a, n in collapsed[:6])
        print(f"[postprocess] collapsed {sum(n for _, _, n in collapsed)} "
              f"acronym mention(s): {preview}", file=sys.stderr)
    try:
        manifest_data = json.loads((book_dir / "manifest.json").read_text(encoding="utf-8"))
        nav_added = offline_postprocess.build_nav_overrides(book_dir, manifest_data)
        if nav_added:
            print(f"[postprocess] wrote {nav_added} bilingual nav label(s)", file=sys.stderr)
        # Chapters whose title lives in a <header> are invisible to build_nav_overrides
        # (strip_non_content drops the header); translate those titles via the model.
        title_added = offline_postprocess.translate_header_titles(
            book_dir, manifest_data, provider
        )
        if title_added:
            print(
                f"[postprocess] translated {title_added} header chapter title(s)",
                file=sys.stderr,
            )
    except Exception as exc:
        print(f"[postprocess] nav override build skipped: {exc}", file=sys.stderr)

    out_epub = out_parent / f"{book_stem}_bilingual.epub"
    print(f"[assemble] -> {out_epub}", file=sys.stderr)
    try:
        # strict_nav=False because minimal glossary has no chapter_titles_zh, so
        # body nav labels would otherwise abort assembly. Audits still run.
        assemble(book_dir=book_dir, out_path=out_epub, strict_nav=False)
    except Exception as exc:
        print(f"[assemble] FAILED: {exc}", file=sys.stderr)
        return_code = 3
        error_summary = f"assemble failed: {exc}"
        duration_sec = time.monotonic() - started
        return {
            "path": book_path,
            "status": "failed",
            "duration_sec": duration_sec,
            "error_summary": error_summary,
            "return_code": return_code,
        }

    audit_warning_records: list[dict[str, str]] = []
    if not args.no_audit:
        print("[audit] running 4 deterministic gates", file=sys.stderr)
        results = run_audits(source=book_path, output=out_epub, book_dir=book_dir)
        print(format_summary(results), file=sys.stderr)
        audit_warning_records = _record_audit_warnings(state, state_path, results)
        if not all_passed(results):
            print("[audit] one or more gates failed; review report above", file=sys.stderr)
            return_code = 4
            error_summary = "audit failed"
            duration_sec = time.monotonic() - started
            return {
                "path": book_path,
                "status": "failed",
                "duration_sec": duration_sec,
                "error_summary": error_summary,
                "return_code": return_code,
                "audit_warnings": len(audit_warning_records),
            }

    warning_suffix = f" (audit: {len(audit_warning_records)} WARN)"
    print(f"[OK] {out_epub}{warning_suffix}", file=sys.stderr)
    duration_sec = time.monotonic() - started
    return {
        "path": book_path,
        "status": "success",
        "duration_sec": duration_sec,
        "error_summary": "",
        "return_code": return_code,
        "audit_warnings": len(audit_warning_records),
    }


def _record_audit_warnings(
    state: dict,
    state_path: Path,
    results: list,
) -> list[dict[str, str]]:
    records = [
        {"audit": result.name, "warning": warning}
        for result in results
        for warning in result.warnings
    ]
    state["audit_warnings"] = {"count": len(records), "findings": records}
    state_mod.save(state_path, state)
    return records


def _print_summary(results: list[dict[str, object]]) -> None:
    success_count = sum(1 for result in results if result["status"] == "success")
    total = len(results)
    print(f"=== Summary === {success_count}/{total} books succeeded", file=sys.stderr)
    for result in results:
        path = result["path"]
        assert isinstance(path, Path)
        status = result["status"]
        duration_sec = float(result["duration_sec"])
        error_summary = result["error_summary"]
        suffix = f" error={error_summary}" if error_summary else ""
        warning_count = int(result.get("audit_warnings", 0))
        if warning_count:
            suffix += f" audit_warnings={warning_count}"
        print(f"- {path.name}: {status} {duration_sec:.1f}s{suffix}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.engine == "ollama" and not args.ollama_model:
        parser.error("--ollama-model is required when --engine=ollama")
    if args.engine == "omlx" and not args.omlx_model:
        parser.error("--omlx-model is required when --engine=omlx")

    # Validate the text dictionary before extraction, provider startup, or GPU
    # work. Some OpenCC distributions can construct s2tw from binary .ocd2 files
    # but do not ship the STCharacters.txt needed by the safe sentence gate.
    converter = offline_postprocess._converter()
    triggers = (
        offline_postprocess._simplified_triggers(converter)
        if converter is not None
        else None
    )
    if triggers is None:
        print(
            "ERROR: safe Simplified->Traditional conversion unavailable; "
            "install opencc-python-reimplemented with STCharacters.txt",
            file=sys.stderr,
        )
        return 2
    print(f"[opencc] simplified_triggers={len(triggers)}", file=sys.stderr)

    results: list[dict[str, object]] = []
    total_books = len(args.book)
    for index, book_path in enumerate(args.book, start=1):
        print(f"=== [{index}/{total_books}] book: {book_path.name} ===", file=sys.stderr)
        book_started = time.monotonic()
        try:
            result = translate_single_book(book_path, args)
        except Exception as exc:
            result = {
                "path": book_path,
                "status": "failed",
                "duration_sec": time.monotonic() - book_started,
                "error_summary": str(exc),
                "return_code": 1,
            }
            print(f"[ERROR {book_path.name}] {exc}", file=sys.stderr)
        else:
            if result["status"] != "success":
                print(f"[ERROR {book_path.name}] {result['error_summary']}", file=sys.stderr)
        results.append(result)

    _print_summary(results)
    return 0 if all(result["status"] == "success" for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())

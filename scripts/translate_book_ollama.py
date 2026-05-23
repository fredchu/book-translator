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
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import chunker  # noqa: E402
import dispatch  # noqa: E402
import extract_epub  # noqa: E402
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
        provider.temperature = 0.5 if attempt == 1 else default_temperature
        try:
            result = provider.translate(
                user_msg,
                request_id=f"{chapter_label}_{chunk_label}_attempt_{attempt}",
                log_dir=log_dir,
                system=system_msg,
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
        )
    except ProviderError as exc:
        return None, [f"{depth_label}: fallback ProviderError: {exc}"]

    raw = result.raw_text or ""
    cleaned = dispatch.strip_known_leak_prefixes(raw).strip()
    if not cleaned:
        warnings.append(f"{depth_label}: fallback empty response")
        return None, warnings
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
        fallback, fallback_warnings = _translate_single_paragraph_fallback(
            provider,
            chunk_paragraphs[0],
            target_lang,
            book_dir,
            chunk_label,
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


def _record_partial_paragraphs(
    *,
    log_path: Path,
    partial_paragraph_count: int,
) -> None:
    entry = json.loads(log_path.read_text(encoding="utf-8"))
    entry["partial_paragraph_count"] = partial_paragraph_count
    log_path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--book", required=True, type=Path, help="path to .epub")
    parser.add_argument("--engine", choices=["ollama", "omlx"], default="omlx",
                        help="default omlx (Qwopus3.6-27B-v2-MLX-4bit) — fastest+highest-quality offline path on M1 Max; ollama+hy-mt2:7b/translategemma:12b are alternates")
    parser.add_argument("--ollama-model", default=None, help="e.g. hy-mt2:7b / translategemma:27b")
    parser.add_argument("--omlx-model", default="Qwopus3.6-27B-v2-MLX-4bit",
                        help="default Qwopus3.6-27B-v2-MLX-4bit (Claude Opus 4.6/4.7 distilled, ~2h13m for 23-chapter book on M1 Max 32GB)")
    parser.add_argument("--out", required=False, type=Path, default=None,
                        help="output parent dir; per-book dir created inside (default: book's parent dir)")
    parser.add_argument("--ollama-host", default="http://localhost:11434")
    parser.add_argument("--omlx-host", default="http://localhost:8090")
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
    parser.add_argument("--no-audit", action="store_true", help="skip the 4 deterministic audits at the end")
    parser.add_argument("--no-resume", action="store_true", help="re-extract + re-translate from scratch")
    parser.add_argument("--limit", type=int, default=None, help="cap on chapters translated this run (debug)")
    args = parser.parse_args()

    if args.engine == "ollama" and not args.ollama_model:
        parser.error("--ollama-model is required when --engine=ollama")
    if args.engine == "omlx" and not args.omlx_model:
        parser.error("--omlx-model is required when --engine=omlx")

    if args.out is None:
        args.out = args.book.parent
    args.out.mkdir(parents=True, exist_ok=True)
    book_stem = args.book.stem
    book_dir = args.out / book_stem
    state_path = book_dir / "state.json"
    manifest_path = book_dir / "manifest.json"

    if args.no_resume or not manifest_path.exists():
        print(f"[extract] {args.book.name} -> {book_dir}", file=sys.stderr)
        extract_epub.extract(args.book, args.out)
    else:
        print(f"[extract] cached at {book_dir} (--no-resume to redo)", file=sys.stderr)

    if state_path.exists() and not args.no_resume:
        state = state_mod.load(state_path)
        assert state is not None
        print(f"[state] resumed from {state_path}", file=sys.stderr)
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        state = state_mod.init_state(args.book, manifest.get("spine", []), args.target_lang)
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
        )
    if not provider.ping():
        print(f"ERROR: {args.engine} unreachable at {selected_host}", file=sys.stderr)
        return 2

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

    carryover = ""
    done_count = 0
    failed_count = 0
    skipped_count = 0
    started = time.monotonic()

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
        provider.temperature = args.temperature  # reset after retry bump

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

    total = time.monotonic() - started
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
        return 0

    out_epub = args.out / f"{book_stem}_bilingual.epub"
    print(f"[assemble] -> {out_epub}", file=sys.stderr)
    try:
        # strict_nav=False because minimal glossary has no chapter_titles_zh, so
        # body nav labels would otherwise abort assembly. Audits still run.
        assemble(book_dir=book_dir, out_path=out_epub, strict_nav=False)
    except Exception as exc:
        print(f"[assemble] FAILED: {exc}", file=sys.stderr)
        return 3

    if not args.no_audit:
        print("[audit] running 4 deterministic gates", file=sys.stderr)
        results = run_audits(source=args.book, output=out_epub, book_dir=book_dir)
        print(format_summary(results), file=sys.stderr)
        if not all_passed(results):
            print("[audit] one or more gates failed; review report above", file=sys.stderr)
            return 4

    print(f"[OK] {out_epub}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

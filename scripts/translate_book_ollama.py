#!/usr/bin/env python3
"""End-to-end book translation driver using a local Ollama model.

Sequential per-chapter loop (single GPU). Phase 1 marker alignment is enforced
on every chapter; misalignment triggers one retry with a higher temperature
before falling back to mark_failed. State.json drives resume; re-running on
the same --out picks up where the previous run stopped.

Pipeline:
    extract_epub.extract → init_state → for each translate spine item:
        build prompt with marker + carryover + minimal glossary
        OllamaProvider.translate (attempt 0)
        strip_known_leak_prefixes + extract_aligned_translation
        on misalign: retry with temperature=0.5 (attempt 1)
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
from providers import OllamaProvider, ProviderError  # noqa: E402


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


def _translate_chunk(
    provider: OllamaProvider,
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
    # Build a synthetic HTML containing just this chunk's paragraphs so the
    # existing dispatch.build_subagent_prompt + html_to_paragraphs pipeline
    # produces a chunk-scoped marker prompt without further refactoring.
    chunk_html = "".join(f"<p>{p}</p>" for p in chunk_paragraphs)
    prompt = dispatch.build_subagent_prompt(
        chapter_label=f"{chapter_label} ({chunk_label})",
        book_title=book_title,
        target_lang=target_lang,
        glossary=MINIMAL_GLOSSARY,
        style_sample="",
        carryover=carryover,
        chapter_html=chunk_html,
    )
    expected_count = len(chunk_paragraphs)
    log_dir = book_dir / "_ollama_logs"
    raw = ""
    warnings: list[str] = []

    for attempt in (0, 1):
        provider.temperature = 0.5 if attempt == 1 else default_temperature
        try:
            result = provider.translate(
                prompt,
                request_id=f"{chapter_label}_{chunk_label}_attempt_{attempt}",
                log_dir=log_dir,
            )
        except ProviderError as exc:
            warnings.append(f"attempt {attempt}: ProviderError: {exc}")
            continue
        raw = result.raw_text
        warnings = dispatch.validate_translation(raw, chunk_html)
        cleaned = dispatch.strip_known_leak_prefixes(raw)
        try:
            aligned = dispatch.extract_aligned_translation(cleaned, expected_count=expected_count)
            return aligned, raw, warnings
        except ValueError as exc:
            warnings.append(f"attempt {attempt}: {exc}")
            continue

    return None, raw, warnings


def _translate_chapter_chunked(
    provider: OllamaProvider,
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
) -> tuple[str | None, list[str]]:
    """Translate a chapter via paragraph chunking. Returns (stitched_or_None, warnings)."""
    paragraphs = dispatch.html_to_paragraphs(html)
    if not paragraphs:
        return "", []
    plan = chunker.chunk_paragraphs(paragraphs, max_chars=chunk_max_chars)

    accumulated: list[str] = []
    chunk_carry = carryover
    all_warnings: list[str] = []

    for i, chunk in enumerate(plan.chunks, start=1):
        chunk_label = f"ck{i:02d}of{len(plan.chunks):02d}"
        aligned, raw, warns = _translate_chunk(
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
            all_warnings.append(
                f"{chunk_label}: chunk failed after 2 attempts ({len(chunk.paragraphs)} paragraphs)"
            )
            return None, all_warnings
        accumulated.append(aligned)
        chunk_carry = aligned[-200:]

    return chunker.stitch(accumulated), all_warnings


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--book", required=True, type=Path, help="path to .epub")
    parser.add_argument("--ollama-model", required=True, help="e.g. hy-mt2:7b / translategemma:27b")
    parser.add_argument("--out", required=True, type=Path, help="output parent dir; per-book dir created inside")
    parser.add_argument("--ollama-host", default="http://localhost:11434")
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

    provider = OllamaProvider(
        model=args.ollama_model,
        host=args.ollama_host,
        timeout=args.timeout,
        num_ctx=args.num_ctx,
        num_predict=args.num_predict,
        temperature=args.temperature,
    )
    if not provider.ping():
        print(f"ERROR: ollama unreachable at {args.ollama_host}", file=sys.stderr)
        return 2

    book_title = args.book_title or book_stem
    chapters = state["chapters"]
    translate_ids = sorted(
        cid for cid, entry in chapters.items() if entry.get("output_strategy") == state_mod.TRANSLATE
    )
    print(
        f"[run] model={args.ollama_model} chapters={len(translate_ids)} target={args.target_lang}",
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
        aligned, warns = _translate_chapter_chunked(
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

        translation_log.write_log_entry(
            book_dir=book_dir,
            chapter_id=cid,
            prompt=f"(see _ollama_logs/{ch_label}_ck*.json)",
            raw_response="(chunked — see _ollama_logs for raw chunks)",
            parsed_translation=aligned or "",
            validation_warnings=warns,
            model=args.ollama_model,
            source_paragraph_count=len(dispatch.html_to_paragraphs(html)),
        )

        if aligned is not None:
            translation_path.write_text(aligned, encoding="utf-8")
            state_mod.mark_done(state, cid, aligned)
            done_count += 1
            carryover = aligned[-200:]
            print(
                f"[{i}/{len(translate_ids)}] {cid} done {elapsed:.1f}s chars={len(aligned)}",
                file=sys.stderr,
            )
        else:
            state_mod.mark_failed(state, cid, "marker misaligned after 2 attempts")
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

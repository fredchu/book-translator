#!/usr/bin/env python3
"""Single-chapter translation CLI for benchmarking and ad-hoc runs.

Reads an EPUB, picks one chapter, runs it through the chosen provider, writes
the raw response + a metadata sidecar. Used by run_benchmark.py for multi-model
comparison and by the main session for spot-checking a chapter in isolation.

Example:
    python3 scripts/translate_chapter_cli.py \\
        --book "/path/to/book.epub" \\
        --chapter 1 \\
        --engine ollama \\
        --ollama-model translategemma:27b \\
        --out runs/my-test/

Defaults to a minimal glossary + empty style/carryover (good enough for
benchmarking; production runs use the full dispatch pipeline).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import dispatch  # noqa: E402
from epub_reader import EPUBReader  # noqa: E402
from providers import provider_factory  # noqa: E402


MINIMAL_GLOSSARY = {
    "characters": {},
    "places": {},
    "terms": {},
    "chapter_titles_zh": {},
    "style_anchor": {
        "register": "literary plain prose",
        "avoid": ["翻譯腔", "過度書面化", "四字結構堆疊"],
        "prefer": ["短句", "口語節奏", "首見並列術語"],
    },
}


def _read_chapter_html(epub_path: Path, chapter_index: int) -> tuple[str, str]:
    """Pull the N-th xhtml spine item out of the EPUB.

    Returns (xhtml_text, item_path). 1-indexed; uses EPUBReader.spine_xhtml_paths()
    which already filters to media-type application/xhtml+xml and excludes nav by
    default. Front matter pages (cover, title) are part of this count, so the
    caller is responsible for picking the correct chapter number.
    """
    with EPUBReader(epub_path) as reader:
        paths = reader.spine_xhtml_paths()
        if not paths:
            raise SystemExit(f"no xhtml spine items in {epub_path}")
        if chapter_index < 1 or chapter_index > len(paths):
            raise SystemExit(
                f"chapter {chapter_index} out of range (1..{len(paths)})"
            )
        chosen = paths[chapter_index - 1]
        html = reader.read(chosen).decode("utf-8", errors="replace")
        return html, chosen


def _build_prompt(chapter_html: str, *, chapter_label: str, book_title: str) -> str:
    return dispatch.build_subagent_prompt(
        chapter_label=chapter_label,
        book_title=book_title,
        target_lang="zh-tw",
        glossary=MINIMAL_GLOSSARY,
        style_sample="",
        carryover="",
        chapter_html=chapter_html,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--book", required=True, type=Path, help="path to .epub")
    parser.add_argument("--chapter", type=int, default=1, help="1-indexed chapter (default 1)")
    parser.add_argument("--engine", default="ollama", choices=["anthropic", "ollama"])
    parser.add_argument("--ollama-model", default=None, help="e.g. translategemma:27b")
    parser.add_argument("--ollama-host", default="http://localhost:11434")
    parser.add_argument("--out", required=True, type=Path, help="output dir")
    parser.add_argument("--book-title", default=None, help="title to inject into prompt")
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--num-ctx", type=int, default=32768)
    parser.add_argument("--num-predict", type=int, default=8192)
    parser.add_argument("--temperature", type=float, default=0.3)
    args = parser.parse_args()

    if args.engine == "ollama" and not args.ollama_model:
        parser.error("--ollama-model is required when --engine=ollama")

    args.out.mkdir(parents=True, exist_ok=True)

    chapter_html, item_name = _read_chapter_html(args.book, args.chapter)
    book_title = args.book_title or args.book.stem
    source_paragraphs = dispatch.html_to_paragraphs(chapter_html)
    prompt = _build_prompt(chapter_html, chapter_label=str(args.chapter), book_title=book_title)

    provider = provider_factory(
        args.engine,
        model=args.ollama_model,
        host=args.ollama_host,
        timeout=args.timeout,
        num_ctx=args.num_ctx,
        num_predict=args.num_predict,
        temperature=args.temperature,
    ) if args.engine == "ollama" else provider_factory(args.engine, model=args.ollama_model)

    if args.engine == "ollama" and not provider.ping():  # type: ignore[attr-defined]
        print(f"ERROR: ollama server unreachable at {args.ollama_host}", file=sys.stderr)
        return 2

    model_slug = (args.ollama_model or "anthropic").replace(":", "_").replace("/", "_")
    request_id = f"ch{args.chapter:02d}_{model_slug}"
    log_dir = args.out / "_logs"

    print(
        f"[run] engine={args.engine} model={args.ollama_model or 'opus'} "
        f"chapter={args.chapter} ({item_name}) source_paragraphs={len(source_paragraphs)}",
        file=sys.stderr,
    )
    started = time.monotonic()
    result = provider.translate(prompt, request_id=request_id, log_dir=log_dir)
    elapsed_s = time.monotonic() - started

    out_text_path = args.out / f"{request_id}.txt"
    out_meta_path = args.out / f"{request_id}.meta.json"
    out_text_path.write_text(result.raw_text, encoding="utf-8")
    out_meta_path.write_text(
        json.dumps(
            {
                "request_id": request_id,
                "engine": args.engine,
                "model": result.model,
                "book": str(args.book),
                "book_title": book_title,
                "chapter": args.chapter,
                "spine_item": item_name,
                "source_paragraphs": len(source_paragraphs),
                "latency_ms": result.latency_ms,
                "elapsed_s_observed": round(elapsed_s, 2),
                "retries": result.retries,
                "metadata": result.metadata,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[done] wrote {out_text_path} ({len(result.raw_text)} chars, {elapsed_s:.1f}s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

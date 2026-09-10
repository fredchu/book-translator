#!/usr/bin/env python3
"""Build adaptive-concurrency probe prompts from the book about to be translated."""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

from ebooklib import epub

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import dispatch  # noqa: E402
from chunker import chunk_paragraphs  # noqa: E402
from content_blocks import extract_paragraphs  # noqa: E402

REQUIRED = 24 + 16 + 12 + 8
LARGE_CHARS = 2500
SEED = 20260910


def prompts_from_epub(book_path: Path) -> list[dict[str, object]]:
    book = epub.read_epub(str(book_path), options={"ignore_ncx": True})
    by_id = {item.get_id(): item for item in book.get_items()}
    prompts: list[dict[str, object]] = []
    for idref, _linear in book.spine:
        item = by_id.get(idref)
        if item is None:
            continue
        try:
            html = item.get_content().decode("utf-8", errors="replace")
        except AttributeError:
            continue
        paragraphs = extract_paragraphs(html)
        if not paragraphs:
            continue
        for index, chunk in enumerate(chunk_paragraphs(paragraphs).chunks):
            system, user = dispatch.build_ollama_chunk_prompt(
                chunk_paragraphs=chunk.paragraphs,
                carryover="",
                fixed_terms={},
            )
            prompts.append(
                {
                    "kind": "chunk",
                    "chapter": str(idref),
                    "idx": index,
                    "system": system,
                    "user": user,
                    "source_chars": sum(len(p) for p in chunk.paragraphs),
                }
            )
    return prompts


def build_load(book_path: Path) -> dict[str, object]:
    all_prompts = prompts_from_epub(book_path)
    large = [row for row in all_prompts if int(row["source_chars"]) >= LARGE_CHARS]
    # Never dilute a short supply of large chunks with small segments: small prompts make
    # tok/s look better and can select an unsafe N. Repeat real large chunks if necessary.
    pool = large if large else all_prompts
    if not pool:
        raise ValueError(f"no translatable chunks found in {book_path}")
    rng = random.Random(SEED)
    rng.shuffle(pool)
    repeated = len(pool) < REQUIRED
    selected: list[dict[str, object]] = []
    while len(selected) < REQUIRED:
        selected.extend(pool)
    selected = selected[:REQUIRED]
    return {
        "book": str(book_path),
        "seed": SEED,
        "required": REQUIRED,
        "large_source_chars": LARGE_CHARS,
        "available_chunks": len(all_prompts),
        "available_large_chunks": len(large),
        "repeated_for_short_book": repeated,
        "requests": selected,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--book", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    payload = build_load(args.book)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    if payload["repeated_for_short_book"]:
        print(
            f"[adaptive-probe] insufficient production-sized chunks "
            f"(large={payload['available_large_chunks']}, total={payload['available_chunks']}); "
            "repetition is unavoidable and recorded in load.json",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

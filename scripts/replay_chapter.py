#!/usr/bin/env python3
"""CLI: re-validate + re-extract a logged chapter's translation without
re-dispatching the subagent.

Usage:
    python3 replay_chapter.py --book-dir <dir> --chapter <chapter_id>
    python3 replay_chapter.py --book-dir <dir> --chapter <chapter_id> \
        --rewrite-translation-file

Without --rewrite-translation-file, prints validation warnings and the parsed
translation to stdout for human review. With the flag, also writes the parsed
translation to `<book_dir>/chapters/<chapter_id>_translation.txt` so a later
assemble.py run picks it up.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import dispatch  # type: ignore
import translation_log as tlog  # type: ignore


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--book-dir", required=True, type=Path)
    p.add_argument("--chapter", required=True, help="chapter_id, e.g. item_005")
    p.add_argument("--rewrite-translation-file", action="store_true")
    args = p.parse_args()

    entry = tlog.read_log_entry(book_dir=args.book_dir, chapter_id=args.chapter)
    if entry is None:
        print(f"No log entry for {args.chapter} in {args.book_dir}/translation_log/",
              file=sys.stderr)
        return 1

    print(f"Chapter: {entry['chapter_id']}")
    print(f"Model:   {entry['model']}")
    print(f"Time:    {entry['timestamp']}")
    print()

    raw = entry["raw_response"]
    expected = entry.get("source_paragraph_count")
    if expected is None:
        print("WARN: log entry has no source_paragraph_count; using marker count as expected.",
              file=sys.stderr)
        expected = entry.get("response_marker_count", 0)

    cleaned = dispatch.strip_known_leak_prefixes(raw)
    try:
        text = dispatch.extract_aligned_translation(cleaned, expected_count=expected)
        print("[OK] Marker alignment passed.")
        print(f"[OK] {len(text.split(chr(10) + chr(10)))} paragraphs in parsed translation.")
    except ValueError as e:
        print(f"[FAIL] {e}")
        print("Use the log file directly to repair the raw response.")
        return 2

    # AUP refusal check
    aup = dispatch.detect_aup_refusal(raw)
    if aup:
        print(f"[NOTE] AUP refusal phrase still present: {aup}")

    if args.rewrite_translation_file:
        out_path = Path(args.book_dir) / "chapters" / f"{args.chapter}_translation.txt"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"[WROTE] {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

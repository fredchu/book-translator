"""Regenerate the Co-Intelligence bilingual EPUB from existing translations."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import assemble  # type: ignore  # noqa: E402
import extract_epub  # type: ignore  # noqa: E402
import state  # type: ignore  # noqa: E402

DEFAULT_SOURCE = Path("/Users/fredchu/Documents/For_Claude/inbox/Co-Intelligence _ Living and Working with AI.epub")
DEFAULT_RUN_DIR = Path("/Users/fredchu/Documents/For_Claude/inbox/co-intelligence-zh-tw/co-intelligence")
DEFAULT_OUTPUT = Path("/Users/fredchu/Documents/For_Claude/inbox/co-intelligence-zh-tw/co-intelligence_bilingual.epub")


def regenerate(source: Path = DEFAULT_SOURCE, run_dir: Path = DEFAULT_RUN_DIR, output: Path = DEFAULT_OUTPUT) -> Path:
    translations = _read_existing_translations(run_dir)
    extract_epub.extract(source, run_dir.parent, min_chars=0, book_stem_override=run_dir.name)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    translate_entries = [
        entry for entry in manifest["spine"]
        if entry.get("role") in {"body", "epilogue"}
    ]
    if len(translate_entries) != len(translations):
        raise ValueError(
            f"expected {len(translations)} body/epilogue spine items, found {len(translate_entries)}"
        )

    for entry in manifest["spine"]:
        role = entry.get("role")
        if role in {"body", "epilogue"}:
            entry["output_strategy"] = "translate"
        elif role == "nav":
            entry["output_strategy"] = "nav_generated"
            entry.pop("translation_id", None)
        else:
            entry["output_strategy"] = "source_only"
            entry.pop("translation_id", None)
        entry.pop("reason", None)

    for entry, (legacy_id, translation_text) in zip(translate_entries, translations):
        entry["translation_id"] = legacy_id
        (run_dir / "chapters" / f"{entry['id']}_translation.txt").write_text(
            translation_text, encoding="utf-8"
        )

    manifest["chapters"] = extract_epub.chapters_from_spine(manifest["spine"])
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    assemble.assemble(run_dir, output)
    _write_completed_state(source, run_dir, manifest)
    return output


def _read_existing_translations(run_dir: Path) -> list[tuple[str, str]]:
    translations: list[tuple[str, str]] = []
    for index in range(3, 14):
        legacy_id = f"ch_{index:02d}"
        path = run_dir / "chapters" / f"{legacy_id}_translation.txt"
        if not path.is_file():
            raise ValueError(f"missing existing translation: {path}")
        translations.append((legacy_id, path.read_text(encoding="utf-8")))
    return translations


def _write_completed_state(source: Path, run_dir: Path, manifest: dict) -> None:
    s = state.init_state(source, manifest["spine"], target_lang="zh-tw")
    s["glossary_built"] = True
    s["style_confirmed"] = True
    for entry in manifest["spine"]:
        strategy = entry["output_strategy"]
        if strategy == "translate":
            translation = (run_dir / "chapters" / f"{entry['id']}_translation.txt").read_text(
                encoding="utf-8"
            )
            state.mark_done(s, entry["id"], translation)
        elif strategy in {"source_only", "nav_generated"}:
            state.mark_source_ready(s, entry["id"])
        elif strategy == "drop_explicit":
            state.mark_dropped(s, entry["id"], entry["reason"])
    state.save(run_dir / "state.json", s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--book-dir", type=Path, default=DEFAULT_RUN_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        out = regenerate(args.source, args.book_dir, args.out)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

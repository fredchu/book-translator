"""Persistence helpers for per-book translation overrides."""

from __future__ import annotations

import json
from pathlib import Path

TRANSLATIONS_EXTRA_FILENAME = "translations_extra.json"


def load(book_dir: Path) -> dict:
    """Load translations_extra.json from a book directory."""
    path = book_dir / TRANSLATIONS_EXTRA_FILENAME
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: translations_extra must be a JSON object")
    for key in ("by_exact_text", "nav_overrides"):
        value = data.get(key)
        if value is not None and not isinstance(value, dict):
            raise ValueError(f"{path}: {key} must be an object")
    return data


def save(book_dir: Path, extra: dict) -> None:
    """Write translations_extra.json with UTF-8 and stable indentation."""
    book_dir.mkdir(parents=True, exist_ok=True)
    (book_dir / TRANSLATIONS_EXTRA_FILENAME).write_text(
        json.dumps(extra, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def write_nav_overrides(
    glossary: dict,
    manifest: dict,
    book_dir: Path,
    *,
    overwrite_existing: bool = False,
) -> dict:
    """Populate translations_extra.json nav_overrides from chapter_titles_zh."""
    titles = glossary.get("chapter_titles_zh", {})
    if titles is None:
        titles = {}
    if not isinstance(titles, dict):
        raise ValueError("glossary chapter_titles_zh must be an object")

    path = book_dir / TRANSLATIONS_EXTRA_FILENAME
    extra = load(book_dir)

    nav_overrides = extra.get("nav_overrides", {})
    if nav_overrides is None:
        nav_overrides = {}
    if not isinstance(nav_overrides, dict):
        raise ValueError(f"{path}: nav_overrides must be an object")
    nav_overrides = dict(nav_overrides)

    for entry in _manifest_spine_entries(manifest):
        if not isinstance(entry, dict):
            continue
        heading = str(entry.get("first_heading") or "")
        if not heading:
            continue
        zh = str(titles.get(heading) or "").strip()
        if not zh:
            continue
        original_idref = str(entry.get("original_idref") or "")
        if not original_idref:
            continue
        if overwrite_existing or original_idref not in nav_overrides:
            nav_overrides[original_idref] = zh

    extra["nav_overrides"] = nav_overrides
    save(book_dir, extra)
    return nav_overrides


def _manifest_spine_entries(manifest: dict) -> list[dict]:
    entries = manifest.get("spine")
    if isinstance(entries, list):
        return entries
    legacy = manifest.get("chapters")
    if isinstance(legacy, list):
        return legacy
    return []

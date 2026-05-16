"""Manifest v2 normalization and legacy chapters[] compatibility helpers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

XHTML_MEDIA_TYPE = "application/xhtml+xml"


@dataclass(frozen=True)
class SpineEntry:
    """Canonical spine entry in manifest.json."""

    id: str
    src_idref: str
    src_href: str
    original_idref: str
    original_path: str
    href: str
    linear: str
    media_type: str
    role: str
    char_count: int
    first_heading: str
    output_strategy: str
    translation_id: str | None = None
    parent_id: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict:
        data = asdict(self)
        if data["reason"] is None:
            data.pop("reason")
        return data


def normalize_entries(manifest: dict | None) -> list[SpineEntry]:
    """Return spine entries with legacy fallback defaults applied."""
    if not manifest:
        return []
    if "spine" in manifest:
        return [_entry_from_mapping(entry, legacy=False) for entry in manifest.get("spine") or []]
    return [_entry_from_mapping(entry, legacy=True) for entry in manifest.get("chapters") or []]


def chapters_from_spine(entries: Iterable[SpineEntry | dict]) -> list[dict]:
    """Build the legacy manifest.json chapters[] compatibility alias."""
    chapters: list[dict] = []
    for item in entries:
        entry = item.as_dict() if isinstance(item, SpineEntry) else item
        if entry.get("output_strategy") != "translate":
            continue
        translation_id = entry.get("translation_id") or entry["id"]
        chapters.append(
            {
                "id": translation_id,
                "spine_id": entry["id"],
                "href": entry["href"],
                "src_href": entry["src_href"],
                "original_idref": entry.get("original_idref", entry.get("src_idref")),
                "original_path": entry.get("original_path", entry.get("src_href")),
                "char_count": entry["char_count"],
                "first_heading": entry["first_heading"],
                "role": entry.get("role", "body"),
                "output_strategy": "translate",
            }
        )
    return chapters


def load(manifest_path: Path) -> dict | None:
    """Read manifest.json from disk as-is, or return None if it is missing."""
    if not manifest_path.is_file():
        return None
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def save(manifest: dict, manifest_path: Path) -> None:
    """Write manifest.json with UTF-8 and stable indentation."""
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _entry_from_mapping(entry: dict, legacy: bool) -> SpineEntry:
    raw_id = str(entry.get("id") or "")
    entry_id = str(entry.get("spine_id") or raw_id) if legacy else raw_id
    src_idref = str(entry.get("src_idref") or entry_id)
    src_href = str(entry.get("src_href") or entry.get("href") or "")
    first_heading = str(entry.get("first_heading") or entry_id)
    role = str(entry.get("role") or _infer_role(
        src_idref=src_idref,
        src_href=src_href,
        first_heading=str(entry.get("first_heading") or ""),
    ))
    translation_id = entry.get("translation_id")
    if legacy:
        translation_id = translation_id or raw_id or entry_id

    return SpineEntry(
        id=entry_id,
        src_idref=src_idref,
        src_href=src_href,
        original_idref=str(entry.get("original_idref") or src_idref or entry_id),
        original_path=str(entry.get("original_path") or src_href or entry.get("href") or ""),
        href=str(entry.get("href") or ""),
        linear=str(entry.get("linear") or "yes"),
        media_type=str(entry.get("media_type") or XHTML_MEDIA_TYPE),
        role=role,
        char_count=_int_or_zero(entry.get("char_count")),
        first_heading=first_heading,
        output_strategy=str(entry.get("output_strategy") or "translate"),
        translation_id=None if translation_id is None else str(translation_id),
        parent_id=None if entry.get("parent_id") is None else str(entry.get("parent_id")),
        reason=None if entry.get("reason") is None else str(entry.get("reason")),
    )


def _infer_role(*, src_idref: str, src_href: str, first_heading: str) -> str:
    try:  # pragma: no cover - import mode depends on caller
        from . import extract_epub
    except ImportError:  # pragma: no cover
        import extract_epub  # type: ignore

    return extract_epub.infer_role(
        src_idref=src_idref,
        src_href=src_href,
        first_heading=first_heading,
    )


def _int_or_zero(value: object) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0

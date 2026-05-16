"""OPF generation for standalone assembled EPUB archives.

This module owns the deterministic package document used by the fallback path.
Source-archive builds preserve the original OPF and never call this builder.
The generated document intentionally mirrors the legacy assemble.py output.
"""

from __future__ import annotations

import html
import posixpath
import re
from pathlib import Path

IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".gif": "image/gif",
    ".svg": "image/svg+xml", ".webp": "image/webp",
}

FONT_MEDIA_TYPES = {
    ".otf": "font/otf", ".ttf": "font/ttf", ".woff": "font/woff", ".woff2": "font/woff2",
}

XHTML_MEDIA_TYPE = "application/xhtml+xml"

try:  # pragma: no cover - import style depends on caller
    from .manifest import entry_original_path
except ImportError:  # pragma: no cover
    from manifest import entry_original_path  # type: ignore


def build_minimal_opf(manifest: dict, entries: list[dict], opf_path: str, nav_path: str | None) -> str:
    """Generate a minimal OPF for the standalone archive path."""
    opf_dir = posixpath.dirname(opf_path)
    manifest_items: list[str] = []
    spine_items: list[str] = []
    for entry in entries:
        uid = html.escape(str(entry.get("original_idref") or entry.get("src_idref") or entry["id"]))
        href = posixpath.relpath(_entry_original_path(entry, opf_path), opf_dir or ".")
        manifest_items.append(f'    <item id="{uid}" href="{html.escape(href)}" media-type="{XHTML_MEDIA_TYPE}"/>')
        spine_items.append(f'    <itemref idref="{uid}"/>')
    if nav_path:
        nav_href = posixpath.relpath(nav_path, opf_dir or ".")
        manifest_items.append(f'    <item id="nav" href="{html.escape(nav_href)}" media-type="{XHTML_MEDIA_TYPE}" properties="nav"/>')
    _append_css_items(manifest_items, manifest)
    _append_font_items(manifest_items, manifest)
    _append_image_items(manifest_items, manifest)
    title = html.escape(str(manifest.get("title") or manifest.get("book_stem") or "Book"))
    identifier = html.escape(f"book-translator-{manifest.get('book_stem', 'book')}")
    return f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">{identifier}</dc:identifier>
    <dc:title>{title}</dc:title>
    <dc:language>zh-Hant</dc:language>
  </metadata>
  <manifest>
{chr(10).join(manifest_items)}
  </manifest>
  <spine>
{chr(10).join(spine_items)}
  </spine>
</package>
"""


def fallback_opf_path() -> str:
    return "OEBPS/content.opf"


def _append_css_items(manifest_items: list[str], manifest: dict) -> None:
    for rel in manifest.get("css_files", []):
        uid = _safe_uid(rel)
        manifest_items.append(f'    <item id="{uid}" href="{html.escape(rel)}" media-type="text/css"/>')


def _append_font_items(manifest_items: list[str], manifest: dict) -> None:
    for rel in manifest.get("font_files", []):
        uid = _safe_uid(rel)
        mt = FONT_MEDIA_TYPES.get(Path(rel).suffix.lower(), "application/octet-stream")
        manifest_items.append(f'    <item id="{uid}" href="{html.escape(rel)}" media-type="{mt}"/>')


def _append_image_items(manifest_items: list[str], manifest: dict) -> None:
    image_names = list(manifest.get("images", []))
    for image in image_names:
        uid = _safe_uid(image)
        mt = IMAGE_MEDIA_TYPES.get(Path(image).suffix.lower(), "application/octet-stream")
        manifest_items.append(f'    <item id="{uid}" href="images/{html.escape(image)}" media-type="{mt}"/>')


def _entry_original_path(entry: dict, opf_path: str | None) -> str:
    return entry_original_path(entry, opf_path)


def _safe_uid(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", value)

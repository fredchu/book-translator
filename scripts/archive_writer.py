"""EPUB archive writers for assembled bilingual output."""

from __future__ import annotations

import html
import posixpath
import zipfile
from pathlib import Path

try:  # pragma: no cover - import style depends on caller
    from .manifest import entry_original_path
    from .nav_builder import nav_path as detect_nav_path
    from .opf_builder import build_minimal_opf
except ImportError:  # pragma: no cover
    from manifest import entry_original_path  # type: ignore
    from nav_builder import nav_path as detect_nav_path  # type: ignore
    from opf_builder import build_minimal_opf  # type: ignore


def write_from_source_archive(
    source_epub: Path,
    out_path: Path,
    replacements: dict[str, bytes],
    compatibility_items: dict[str, bytes],
) -> None:
    """Write an EPUB by copying the source archive and overlaying replacements."""
    with zipfile.ZipFile(source_epub) as src, zipfile.ZipFile(out_path, "w") as out:
        names = src.namelist()
        if "mimetype" in names:
            out.writestr("mimetype", src.read("mimetype"), compress_type=zipfile.ZIP_STORED)
        for name in names:
            if name == "mimetype" or name in replacements:
                continue
            out.writestr(name, src.read(name))
        for name, content in replacements.items():
            out.writestr(name, content)
        for name, content in compatibility_items.items():
            if name not in replacements:
                out.writestr(name, content)


def write_standalone_archive(
    book_dir: Path,
    out_path: Path,
    manifest: dict,
    entries: list[dict],
    replacements: dict[str, bytes],
    compatibility_items: dict[str, bytes],
    opf_path: str,
) -> None:
    """Write the fallback EPUB archive when the source EPUB is not accessible."""
    opf_dir = posixpath.dirname(opf_path)
    with zipfile.ZipFile(out_path, "w") as out:
        _write_mimetype(out)
        _write_container(out, opf_path)
        _write_spine_entries(out, entries, replacements, opf_path)
        _write_compatibility_items(out, compatibility_items)
        _write_assets(out, book_dir, manifest, opf_dir)
        nav_path = detect_nav_path(manifest, opf_path)
        if nav_path and nav_path in replacements:
            out.writestr(nav_path, replacements[nav_path])
        out.writestr(opf_path, build_minimal_opf(manifest, entries, opf_path, nav_path))


def _write_mimetype(out: zipfile.ZipFile) -> None:
    out.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)


def _write_container(out: zipfile.ZipFile, opf_path: str) -> None:
    out.writestr(
        "META-INF/container.xml",
        f"""<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="{html.escape(opf_path)}" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
""",
    )


def _write_spine_entries(
    out: zipfile.ZipFile,
    entries: list[dict],
    replacements: dict[str, bytes],
    opf_path: str,
) -> None:
    for entry in entries:
        path = _entry_original_path(entry, opf_path)
        out.writestr(path, replacements[path])


def _write_compatibility_items(out: zipfile.ZipFile, compatibility_items: dict[str, bytes]) -> None:
    for name, content in compatibility_items.items():
        out.writestr(name, content)


def _write_assets(out: zipfile.ZipFile, book_dir: Path, manifest: dict, opf_dir: str) -> None:
    for rel in manifest.get("css_files", []):
        path = book_dir / rel
        if path.is_file():
            out.writestr(posixpath.join(opf_dir, rel), path.read_bytes())
    for rel in manifest.get("font_files", []):
        path = book_dir / rel
        if path.is_file():
            out.writestr(posixpath.join(opf_dir, rel), path.read_bytes())
    for image in manifest.get("images", []):
        path = book_dir / "images" / image
        if path.is_file():
            out.writestr(posixpath.join(opf_dir, "images", image), path.read_bytes())
    images_dir = book_dir / "images"
    if images_dir.is_dir():
        manifest_images = set(manifest.get("images", []))
        for path in sorted(images_dir.iterdir()):
            if path.is_file() and path.name not in manifest_images:
                out.writestr(posixpath.join(opf_dir, "images", path.name), path.read_bytes())


def _entry_original_path(entry: dict, opf_path: str | None) -> str:
    return entry_original_path(entry, opf_path)

"""Extract an EPUB OPF spine into per-item HTML + a manifest v2.

Output layout:
    <out_dir>/<book_stem>/
        source.opf
        manifest.json
        css/
        fonts/
        images/
        xhtml/
        chapters/
            item_001.html
            item_002.html
            ...

manifest.json schema v2:
    {
        "book_stem": "animal_farm",
        "title": "Animal Farm",
        "authors": ["George Orwell"],
        "language": "en",
        "source_epub": "...",
        "cover": "cover.jpg",
        "spine": [
            {"id": "item_001", "src_idref": "chap01", "src_href": "OEBPS/ch01.xhtml",
             "href": "chapters/item_001.html", "linear": "yes",
             "media_type": "application/xhtml+xml", "role": "body",
             "char_count": 1234, "first_heading": "Chapter 1",
             "output_strategy": "translate", "translation_id": "ch_01",
             "parent_id": null},
            ...
        ]
    }

The OPF spine is the source of truth. ``--min-chars`` is retained only for CLI
compatibility; it no longer filters extraction.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import sys
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

from bs4 import BeautifulSoup
from ebooklib import epub


@dataclass
class SpineEntry:
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


TRANSLATE_ROLES = {"body", "epilogue", "acknowledgments", "about_author"}
PART_CHILD_ROLES = {"body", "epilogue", "acknowledgments", "about_author", "notes"}
PART_CHAIN_BREAK_ROLES = {"cover", "title_page", "copyright", "dedication", "contents", "promo"}
SOURCE_ONLY_ROLES = {
    "cover",
    "title_page",
    "copyright",
    "dedication",
    "contents",
    "part_divider",
    "promo",
    "notes",
}
_HEADING_MAX_LEN = 120
_ROLE_TO_HEADING = {
    "cover": "Cover",
    "title_page": "Title Page",
    "copyright": "Copyright",
    "dedication": "Dedication",
    "contents": "Contents",
    "notes": "Notes",
    "about_author": "About the Author",
    "acknowledgments": "Acknowledgments",
    "promo": "Promotional",
    "nav": "Table of Contents",
}


def extract(
    epub_path: Path,
    out_dir: Path,
    min_chars: int = 200,
    book_stem_override: str | None = None,
) -> Path:
    """Extract every OPF spine item; return the per-book output directory."""
    book = epub.read_epub(str(epub_path), options={"ignore_ncx": True})
    book_stem = book_stem_override or epub_path.stem
    book_out = out_dir / book_stem
    chapters_dir = book_out / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)

    title = _first_meta(book, "title") or book_stem
    authors = [v for v, _ in book.get_metadata("DC", "creator")]
    language = _first_meta(book, "language") or "und"
    opf_path, opf_manifest = _read_opf_manifest(epub_path)
    _copy_source_opf(epub_path, book_out, opf_path)
    css_files = _extract_tree(epub_path, book_out, opf_path, "css")
    font_files = _extract_tree(epub_path, book_out, opf_path, "fonts")
    cover_filename = _extract_cover(epub_path, book_out)
    images = _extract_images(epub_path, book_out)

    entries: list[SpineEntry] = []
    translate_counter = 0
    current_part_id: str | None = None
    for index, (src_idref, linear) in enumerate(_spine_idrefs(book), start=1):
        item = book.get_item_with_id(src_idref)
        opf_item = opf_manifest.get(src_idref, {})
        if item is None:
            continue
        media_type = str(opf_item.get("media_type") or getattr(item, "media_type", ""))
        html = item.get_content().decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)
        item_id = f"item_{index:03d}"
        out_path = chapters_dir / f"{item_id}.html"
        out_path.write_text(html, encoding="utf-8")
        src_href = str(opf_item.get("href") or item.get_name())
        original_path = src_href
        _copy_original_xhtml(epub_path, book_out, original_path, opf_path)
        heading = _first_heading(soup)
        fallback_heading = heading or _first_text(text) or "(untitled)"
        role = infer_role(
            src_idref=src_idref,
            src_href=src_href,
            first_heading=fallback_heading,
            properties=str(opf_item.get("properties") or ""),
        )
        first_heading = heading or _ROLE_TO_HEADING.get(role) or fallback_heading
        strategy = default_output_strategy(role, char_count=len(text))
        if strategy == "translate" and role == "body" and len(text) == 0:
            strategy = "source_only"
        translation_id = None
        if strategy == "translate":
            translate_counter += 1
            translation_id = f"ch_{translate_counter:02d}"
        parent_id = current_part_id if role in PART_CHILD_ROLES else None
        entries.append(
            SpineEntry(
                id=item_id,
                src_idref=src_idref,
                src_href=src_href,
                original_idref=src_idref,
                original_path=original_path,
                href=f"chapters/{item_id}.html",
                linear=linear,
                media_type=media_type or "application/xhtml+xml",
                role=role,
                char_count=len(text),
                first_heading=first_heading,
                output_strategy=strategy,
                translation_id=translation_id,
                parent_id=parent_id,
            )
        )
        if role == "part_divider":
            current_part_id = item_id
        elif role in PART_CHAIN_BREAK_ROLES:
            current_part_id = None

    spine = [asdict(e) for e in entries]
    manifest = {
        "book_stem": book_stem,
        "title": title,
        "authors": authors,
        "language": language,
        "source_epub": str(epub_path),
        "cover": cover_filename,
        "css_files": css_files,
        "font_files": font_files,
        "images": images,
        "images_extracted": len(images),
        "opf_path": opf_path,
        "spine": spine,
        # Compatibility alias for current dispatch/e2e callers. The full spine
        # above is authoritative; this list contains only translate-strategy
        # items and uses legacy ch_NN ids for translation files.
        "chapters": chapters_from_spine(spine),
    }
    (book_out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return book_out


def chapters_from_spine(spine: list[dict]) -> list[dict]:
    chapters: list[dict] = []
    for entry in spine:
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


def infer_role(*, src_idref: str, src_href: str, first_heading: str, properties: str = "") -> str:
    haystack = f"{src_idref} {src_href}".lower()
    heading = first_heading.strip()
    props = properties.lower().split()
    if "nav" in props or src_href.lower().endswith("nav.xhtml"):
        return "nav"
    if "cover" in haystack:
        return "cover"
    if "title" in haystack and "chapter" not in haystack:
        return "title_page"
    if "copyright" in haystack:
        return "copyright"
    if "dedication" in haystack:
        return "dedication"
    heading_first40 = heading[:40].lower()
    if (
        heading.startswith("Copyright ©")
        or heading.startswith("Copyright (c)")
        or "all rights reserved" in heading_first40
    ):
        return "copyright"
    if (
        heading.startswith("To my ")
        or heading.startswith("To the ")
        or heading.startswith("Dedicated to ")
        or heading.startswith("In memory of ")
        or heading.startswith("For my ")
        or heading.startswith("For the ")
    ):
        return "dedication"
    if "contents" in haystack or "toc" in haystack:
        return "contents"
    if re.match(r"^\s*part\s*([ivxlcdm]+|\d+)\b", heading, flags=re.IGNORECASE):
        return "part_divider"
    if "epilogue" in haystack:
        return "epilogue"
    if "acknowledg" in haystack:
        return "acknowledgments"
    if "note" in haystack:
        return "notes"
    if "author" in haystack:
        return "about_author"
    if "next-reads" in haystack or "promo" in haystack:
        return "promo"
    return "body"


def default_output_strategy(role: str, char_count: int = 0) -> str:
    if role in {"part_divider", "copyright", "dedication"} and char_count >= 300:
        return "translate"
    if role in TRANSLATE_ROLES:
        return "translate"
    if role == "nav":
        return "nav_generated"
    if role in SOURCE_ONLY_ROLES:
        return "source_only"
    return "translate"


def _first_meta(book: epub.EpubBook, name: str) -> str | None:
    pairs = book.get_metadata("DC", name)
    if not pairs:
        return None
    return pairs[0][0]


def _spine_idrefs(book: epub.EpubBook) -> list[tuple[str, str]]:
    idrefs: list[tuple[str, str]] = []
    for raw in book.spine:
        if isinstance(raw, tuple):
            src_idref = str(raw[0])
            linear = str(raw[1] or "yes")
        else:
            src_idref = str(raw)
            linear = "yes"
        idrefs.append((src_idref, linear))
    return idrefs


def _read_opf_manifest(epub_path: Path) -> tuple[str | None, dict[str, dict[str, str]]]:
    with zipfile.ZipFile(epub_path) as z:
        opf_path = _find_opf_path(z)
        if not opf_path:
            return None, {}
        soup = BeautifulSoup(z.read(opf_path), "lxml-xml")
        opf_dir = posixpath.dirname(opf_path)
        manifest: dict[str, dict[str, str]] = {}
        for item in soup.find_all("item"):
            item_id = str(item.get("id") or "")
            if not item_id:
                continue
            href = str(item.get("href") or "")
            src_href = posixpath.normpath(posixpath.join(opf_dir, href)) if opf_dir else href
            manifest[item_id] = {
                "href": src_href,
                "media_type": str(item.get("media-type") or ""),
                "properties": str(item.get("properties") or ""),
            }
        return opf_path, manifest


def _find_opf_path(z: zipfile.ZipFile) -> str | None:
    try:
        soup = BeautifulSoup(z.read("META-INF/container.xml"), "lxml-xml")
    except KeyError:
        soup = None
    if soup is not None:
        rootfile = soup.find("rootfile", attrs={"media-type": "application/oebps-package+xml"})
        if rootfile and rootfile.get("full-path"):
            return str(rootfile.get("full-path"))
    return next((n for n in z.namelist() if n.lower().endswith(".opf")), None)


def _copy_source_opf(epub_path: Path, book_out: Path, opf_path: str | None) -> None:
    if not opf_path:
        return
    with zipfile.ZipFile(epub_path) as z:
        if opf_path in z.namelist():
            (book_out / "source.opf").write_bytes(z.read(opf_path))


def _copy_original_xhtml(epub_path: Path, book_out: Path, original_path: str, opf_path: str | None) -> None:
    if not original_path:
        return
    local_path = _local_book_path(original_path, opf_path)
    out_path = book_out / local_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(epub_path) as z:
        if original_path in z.namelist():
            out_path.write_bytes(z.read(original_path))


def _extract_tree(epub_path: Path, book_out: Path, opf_path: str | None, dirname: str) -> list[str]:
    """Copy files under the EPUB OPF sibling directory into book_out/<dirname>/."""
    copied: list[str] = []
    with zipfile.ZipFile(epub_path) as z:
        opf_dir = posixpath.dirname(opf_path or "")
        prefix = f"{opf_dir}/{dirname}/" if opf_dir else f"{dirname}/"
        for name in z.namelist():
            if name.endswith("/") or not name.startswith(prefix):
                continue
            rel = posixpath.relpath(name, opf_dir) if opf_dir else name
            out_path = book_out / rel
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(z.read(name))
            copied.append(rel)
    return sorted(copied)


def _extract_images(epub_path: Path, book_out: Path) -> list[str]:
    """Copy every EPUB image into ``book_out/images/`` with no cover skip.

    Filenames are flattened (just the basename, no zip directory tree). The
    cover image is intentionally kept here too because cover XHTML pages often
    reference the original image basename while ``set_cover`` stores metadata
    separately.
    """
    images_dir = book_out / "images"
    images_dir.mkdir(exist_ok=True)
    copied: list[str] = []
    with zipfile.ZipFile(epub_path) as z:
        for name in z.namelist():
            lower = name.lower()
            if not any(lower.endswith(e) for e in (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp")):
                continue
            basename = Path(name).name
            (images_dir / basename).write_bytes(z.read(name))
            copied.append(basename)
    return sorted(copied)


def _local_book_path(original_path: str, opf_path: str | None) -> str:
    opf_dir = posixpath.dirname(opf_path or "")
    if opf_dir and original_path.startswith(f"{opf_dir}/"):
        return posixpath.relpath(original_path, opf_dir)
    return original_path


def _extract_cover(epub_path: Path, book_out: Path) -> str | None:
    """Extract the declared cover image from an EPUB and save to book_out/cover.<ext>."""
    with zipfile.ZipFile(epub_path) as z:
        opf_name = _find_opf_path(z)
        if not opf_name:
            return None
        soup = BeautifulSoup(z.read(opf_name), "lxml-xml")

        cover_href: str | None = None
        for item in soup.find_all("item"):
            props = str(item.get("properties") or "").split()
            if "cover-image" in props:
                href = item.get("href")
                cover_href = str(href) if href else None
                break

        if not cover_href:
            meta = soup.find("meta", attrs={"name": "cover"})
            if meta and meta.get("content"):
                target_id = str(meta.get("content"))
                item = soup.find("item", attrs={"id": target_id})
                if item:
                    href = item.get("href")
                    cover_href = str(href) if href else None

        if not cover_href:
            for item in soup.find_all("item"):
                iid = str(item.get("id") or "").lower()
                mt = str(item.get("media-type") or "").lower()
                if "cover" in iid and mt.startswith("image/"):
                    href = item.get("href")
                    cover_href = str(href) if href else None
                    break

        if not cover_href:
            return None

        opf_dir = str(Path(opf_name).parent)
        cover_path_in_zip = (
            f"{opf_dir}/{cover_href}" if opf_dir and opf_dir != "." else cover_href
        )
        if cover_path_in_zip not in z.namelist():
            return None
        ext = Path(cover_href).suffix or ".jpg"
        out_name = f"cover{ext.lower()}"
        (book_out / out_name).write_bytes(z.read(cover_path_in_zip))
        return out_name


def _first_heading(soup: BeautifulSoup) -> str | None:
    for tag in ("h1", "h2", "h3", "h4"):
        node = soup.find(tag)
        if node:
            text = node.get_text(" ", strip=True)
            text = re.sub(r"\s+", " ", text).strip()
            if 0 < len(text) <= _HEADING_MAX_LEN and not _looks_like_body_prose_heading(text):
                return text
    return None


def _looks_like_body_prose_heading(text: str) -> bool:
    return len(re.findall(r"[.!?。？！]", text)) > 1


def _first_text(text: str) -> str | None:
    collapsed = re.sub(r"\s+", " ", text).strip()
    if not collapsed:
        return None
    return collapsed[:80]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("epub_path", type=Path)
    parser.add_argument("--out", dest="out_dir", type=Path, required=True)
    parser.add_argument("--min-chars", type=int, default=200)
    args = parser.parse_args(argv)

    if not args.epub_path.is_file():
        print(f"not a file: {args.epub_path}", file=sys.stderr)
        return 2
    book_out = extract(args.epub_path, args.out_dir, args.min_chars)
    print(book_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Assemble a bilingual EPUB from a full-spine manifest v2.

For each manifest spine item:
    translate      -> require a translation file and emit bilingual content
    source_only    -> emit source content unchanged except image path rewriting
    nav_generated  -> omit the source nav and let ebooklib generate nav/NCX
    drop_explicit  -> omit only with a non-empty reason

Missing translations for translate-strategy items are hard errors. No EPUB is
written until all strategies and translation inputs pass preflight.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup
from ebooklib import epub

# Make sibling-module import work whether invoked as a script or as `python -m scripts.assemble`.
sys.path.insert(0, str(Path(__file__).parent))
from dispatch import html_to_blocks  # type: ignore  # noqa: E402

CSS = """\
p.src { color: #555; font-style: italic; margin: 0.4em 0 0.1em 0; }
p.tgt { color: #111; margin: 0.1em 0 0.9em 0; }
p.unpaired { color: #b00; }
p.image { text-align: center; margin: 1.2em 0; }
p.image img { max-width: 100%; height: auto; }
h1, h2 { margin-top: 1.2em; }
img { max-width: 100%; height: auto; }
"""

IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".gif": "image/gif",
    ".svg": "image/svg+xml", ".webp": "image/webp",
}

VALID_STRATEGIES = {"translate", "source_only", "nav_generated", "drop_explicit"}


def assemble(book_dir: Path, out_path: Path) -> Path:
    manifest = json.loads((book_dir / "manifest.json").read_text(encoding="utf-8"))
    spine_entries = _manifest_spine(manifest)
    translation_paths = _preflight(book_dir, spine_entries)

    title = manifest["title"]
    authors = manifest.get("authors", [])
    language = manifest.get("language", "und")

    out_book = epub.EpubBook()
    out_book.set_identifier(f"book-translator-{manifest['book_stem']}")
    out_book.set_title(f"{title} (bilingual)")
    out_book.set_language("zh-Hant")
    for author in authors:
        out_book.add_author(author)

    cover_filename = manifest.get("cover")
    if cover_filename:
        cover_path = book_dir / cover_filename
        if cover_path.is_file():
            out_book.set_cover(cover_filename, cover_path.read_bytes())

    style_item = epub.EpubItem(
        uid="style_default", file_name="style/default.css",
        media_type="text/css", content=CSS.encode("utf-8"),
    )
    out_book.add_item(style_item)

    images_dir = book_dir / "images"
    used_images: set[str] = set()
    available_images: dict[str, Path] = {}
    if images_dir.is_dir():
        for img_path in images_dir.iterdir():
            if img_path.is_file():
                available_images[img_path.name] = img_path

    spine: list = ["nav"]
    toc: list = []
    warnings: list[str] = []
    translate_index = 0

    for entry in spine_entries:
        strategy = entry["output_strategy"]
        if strategy == "nav_generated":
            continue
        if strategy == "drop_explicit":
            warnings.append(f"{entry['id']}: dropped explicitly: {entry.get('reason', '')}")
            continue

        src_html = (book_dir / entry["href"]).read_text(encoding="utf-8")
        title_text = entry.get("first_heading") or entry["id"]
        if strategy == "translate":
            translate_index += 1
            translation = translation_paths[entry["id"]].read_text(encoding="utf-8").strip()
            item_html, item_warnings, item_image_refs = _build_chapter_html(
                chap_id=entry.get("translation_id") or entry["id"], heading=title_text,
                src_html=src_html, translation=translation,
            )
            warnings.extend(item_warnings)
            file_name = _output_file_name(entry, translate_index)
        else:
            item_html, item_image_refs = _build_source_only_html(src_html)
            file_name = f"items/{entry['id']}.xhtml"

        used_images.update(item_image_refs)
        epub_item = epub.EpubHtml(
            uid=entry["id"], file_name=file_name, lang=language, title=title_text,
        )
        epub_item.content = item_html
        epub_item.add_item(style_item)
        out_book.add_item(epub_item)
        spine.append(epub_item)
        toc.append(epub.Link(file_name, title_text, entry["id"]))

    for img_name in sorted(used_images):
        if img_name not in available_images:
            warnings.append(f"image referenced by a represented spine item but not in images/: {img_name}")
            continue
        ext = Path(img_name).suffix.lower()
        media_type = IMAGE_MEDIA_TYPES.get(ext, "application/octet-stream")
        img_item = epub.EpubItem(
            uid=f"img_{_safe_uid(img_name)}",
            file_name=f"images/{img_name}",
            media_type=media_type,
            content=available_images[img_name].read_bytes(),
        )
        out_book.add_item(img_item)

    out_book.toc = list(toc)
    out_book.add_item(epub.EpubNcx())
    out_book.add_item(epub.EpubNav())
    out_book.spine = spine

    out_path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(out_path), out_book)

    if warnings:
        print("Assembly warnings:", file=sys.stderr)
        for w in warnings:
            print(f"  - {w}", file=sys.stderr)
    return out_path


def _manifest_spine(manifest: dict) -> list[dict]:
    if "spine" in manifest:
        return [dict(entry) for entry in manifest["spine"]]
    legacy = []
    for entry in manifest.get("chapters", []):
        copied = dict(entry)
        copied.setdefault("src_idref", copied["id"])
        copied.setdefault("src_href", copied.get("src_href", copied.get("href", "")))
        copied.setdefault("linear", "yes")
        copied.setdefault("media_type", "application/xhtml+xml")
        copied.setdefault("role", "body")
        copied.setdefault("first_heading", copied["id"])
        copied.setdefault("output_strategy", "translate")
        copied.setdefault("translation_id", copied["id"])
        legacy.append(copied)
    return legacy


def _preflight(book_dir: Path, spine_entries: list[dict]) -> dict[str, Path]:
    translation_paths: dict[str, Path] = {}
    errors: list[str] = []
    for entry in spine_entries:
        strategy = entry.get("output_strategy")
        if strategy not in VALID_STRATEGIES:
            errors.append(f"{entry.get('id', '(unknown)')}: unknown output_strategy {strategy!r}")
            continue
        if strategy == "drop_explicit" and not str(entry.get("reason", "")).strip():
            errors.append(f"{entry['id']}: drop_explicit requires a non-empty reason")
        if strategy in {"translate", "source_only"} and not (book_dir / entry["href"]).is_file():
            errors.append(f"{entry['id']}: source html missing: {entry['href']}")
        if strategy == "translate":
            path = _translation_path(book_dir, entry)
            if path is None:
                item_path = book_dir / "chapters" / f"{entry['id']}_translation.txt"
                legacy_id = entry.get("translation_id")
                detail = str(item_path)
                if legacy_id:
                    detail += f" or {book_dir / 'chapters' / f'{legacy_id}_translation.txt'}"
                errors.append(f"{entry['id']}: missing translation for translate item ({detail})")
            else:
                translation_paths[entry["id"]] = path
    if errors:
        raise ValueError("Assembly preflight failed:\n" + "\n".join(f"- {e}" for e in errors))
    return translation_paths


def _translation_path(book_dir: Path, entry: dict) -> Path | None:
    item_path = book_dir / "chapters" / f"{entry['id']}_translation.txt"
    if item_path.is_file():
        return item_path
    translation_id = entry.get("translation_id")
    if translation_id:
        legacy_path = book_dir / "chapters" / f"{translation_id}_translation.txt"
        if legacy_path.is_file():
            return legacy_path
    return None


def _output_file_name(entry: dict, translate_index: int) -> str:
    translation_id = entry.get("translation_id")
    if translation_id:
        return f"chapters/{translation_id}.xhtml"
    return f"chapters/ch_{translate_index:02d}.xhtml"


def _build_chapter_html(
    *, chap_id: str, heading: str, src_html: str, translation: str
) -> tuple[str, list[str], set[str]]:
    """Build per-chapter bilingual xhtml."""
    blocks = html_to_blocks(src_html)
    if blocks and blocks[0].get("type") == "text" and blocks[0].get("text") == heading:
        blocks = blocks[1:]

    src_text_blocks = [b for b in blocks if b["type"] == "text"]
    tgt_paras = [p.strip() for p in translation.split("\n\n") if p.strip()]
    warnings: list[str] = []
    pairs = min(len(src_text_blocks), len(tgt_paras))
    if len(src_text_blocks) != len(tgt_paras):
        warnings.append(
            f"{chap_id}: paragraph count mismatch (src_text={len(src_text_blocks)} tgt={len(tgt_paras)}); "
            f"pairing first {pairs}, remainder appended as unpaired"
        )

    image_refs: set[str] = set()
    body_parts: list[str] = [f"<h1>{_escape(heading)}</h1>"]
    text_idx = 0
    for block in blocks:
        if block["type"] == "image":
            src_name = block["src"]
            image_refs.add(src_name)
            alt = _escape(block.get("alt", ""))
            body_parts.append(f'<p class="image"><img src="images/{src_name}" alt="{alt}"/></p>')
        else:
            if text_idx < pairs:
                body_parts.append(f'<p class="src">{_escape(block["text"])}</p>')
                body_parts.append(f'<p class="tgt">{_escape(tgt_paras[text_idx])}</p>')
            else:
                body_parts.append(
                    f'<p class="src unpaired">[unpaired source] {_escape(block["text"])}</p>'
                )
            text_idx += 1

    for extra in tgt_paras[pairs:]:
        body_parts.append(f'<p class="tgt unpaired">[unpaired translation] {_escape(extra)}</p>')

    return "\n".join(body_parts), warnings, image_refs


def _build_source_only_html(src_html: str) -> tuple[str, set[str]]:
    soup = BeautifulSoup(src_html, "html.parser")
    image_refs: set[str] = set()
    root = soup.body or soup
    for img in root.find_all("img"):
        src = str(img.get("src") or "")
        if not src or src.startswith("data:"):
            continue
        basename = Path(src).name
        if not basename:
            continue
        image_refs.add(basename)
        img["src"] = f"images/{basename}"
    return root.decode_contents() if getattr(root, "name", None) == "body" else str(root), image_refs


def _safe_uid(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", value)


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&#39;")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book", dest="book_dir", type=Path, required=True,
                        help="Path to <book_stem>/ directory containing manifest.json")
    parser.add_argument("--out", dest="out_path", type=Path, required=True)
    args = parser.parse_args(argv)

    if not (args.book_dir / "manifest.json").is_file():
        print(f"manifest.json missing in {args.book_dir}", file=sys.stderr)
        return 2
    try:
        out = assemble(args.book_dir, args.out_path)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

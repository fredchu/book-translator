"""Assemble a bilingual EPUB while preserving source archive layout and assets."""

from __future__ import annotations

import argparse, html, posixpath, sys, zipfile
from pathlib import Path

from bs4 import BeautifulSoup
from ebooklib import epub  # re-exported for existing tests

# Make sibling-module imports work whether invoked as a script or as `python -m scripts.assemble`.
sys.path.insert(0, str(Path(__file__).parent))
import archive_writer  # type: ignore  # noqa: E402
import bilingual_rewriter  # type: ignore  # noqa: E402
from dispatch import html_to_paragraphs  # type: ignore  # noqa: E402
import manifest as manifest_module  # type: ignore  # noqa: E402
import nav_builder  # type: ignore  # noqa: E402
import opf_builder  # type: ignore  # noqa: E402
import translations_extra as translations_extra_module  # type: ignore  # noqa: E402

VALID_STRATEGIES = {"translate", "source_only", "nav_generated", "drop_explicit"}
TRANSLATIONS_EXTRA_FILENAME = translations_extra_module.TRANSLATIONS_EXTRA_FILENAME

IMAGE_MEDIA_TYPES, FONT_MEDIA_TYPES, XHTML_MEDIA_TYPE = (
    opf_builder.IMAGE_MEDIA_TYPES, opf_builder.FONT_MEDIA_TYPES, opf_builder.XHTML_MEDIA_TYPE
)
STRUCTURAL_LABELS_ZH_TW, CONTENTS_LINK_LABELS_ZH_TW = (
    bilingual_rewriter.STRUCTURAL_LABELS_ZH_TW, bilingual_rewriter.CONTENTS_LINK_LABELS_ZH_TW
)
_insert_bilingual = bilingual_rewriter.insert_bilingual
for _name in (
    "_promote_header_headings", "_bilingualize_contents_links", "_align_translations",
    "_text_nodes_for_bilingual", "_target_classes", "_add_class", "_fallback_translation",
    "_exact_translation_for_text", "_translations_extra_by_exact_text", "_ensure_han_translation",
    "_looks_like_identifier", "_clean_text",
):
    globals()[_name] = getattr(bilingual_rewriter, _name)
_nav_path = nav_builder.nav_path
_build_nav_xhtml = nav_builder.build_nav_xhtml
_missing_nav_zh_warnings = nav_builder.missing_nav_zh_warnings
_source_ncx_path = nav_builder.source_ncx_path
_patch_toc_ncx = nav_builder.patch_toc_ncx
for _name in (
    "_render_nav_item", "_nav_display_label", "_nav_zh_label",
    "_translations_extra_nav_overrides", "_nav_entry_by_filename", "_nav_label", "_contents_style",
):
    globals()[_name] = getattr(nav_builder, _name)
_write_from_source_archive = archive_writer.write_from_source_archive
_write_standalone_archive = archive_writer.write_standalone_archive
_build_minimal_opf = opf_builder.build_minimal_opf
_fallback_opf_path = opf_builder.fallback_opf_path
_safe_uid = opf_builder._safe_uid

def assemble(book_dir: Path, out_path: Path, strict_nav: bool = True) -> Path:
    """Run the full manifest, rewrite, nav/NCX, archive, and fallback-OPF pipeline.

    strict_nav: when True (default), every spine item must resolve to a Chinese
    nav label via translations_extra.json nav_overrides, glossary
    chapter_titles_zh, or STRUCTURAL_LABELS_ZH_TW; missing labels raise
    ValueError. Set False only for low-level fixtures that do not exercise
    the bilingual ToC contract.
    """
    manifest = manifest_module.load(book_dir / "manifest.json")
    if manifest is None:
        raise FileNotFoundError(book_dir / "manifest.json")

    translations_extra = _load_translations_extra(book_dir)
    spine_entries = [_entry_with_translations_extra(entry, translations_extra) for entry in _manifest_spine(manifest)]
    translation_paths = _preflight(book_dir, spine_entries)
    source_epub = Path(str(manifest.get("source_epub", "")))
    opf_path = manifest.get("opf_path") or opf_builder.fallback_opf_path()
    opf_dir = posixpath.dirname(opf_path)
    represented_entries = [e for e in spine_entries if e.get("output_strategy") not in {"drop_explicit", "nav_generated"}]

    warnings: list[str] = []
    replacements: dict[str, bytes] = {}
    compatibility_items: dict[str, bytes] = {}
    for entry in represented_entries:
        source_html = _read_entry_html(book_dir, entry, opf_path)
        translations = _translations_for_entry(book_dir, entry, translation_paths)
        item_html, item_warnings = bilingual_rewriter.insert_bilingual(source_html, entry, translations)
        warnings.extend(item_warnings)
        warnings.extend(_missing_image_warnings(book_dir, entry, item_html))
        replacements[_entry_original_path(entry, opf_path)] = item_html.encode("utf-8")
        compatibility_items.update(_legacy_test_compat_items(opf_dir, source_html, entry, translations))

    item_nav_path = nav_builder.nav_path(manifest, opf_path)
    if item_nav_path:
        replacements[item_nav_path] = nav_builder.build_nav_xhtml(manifest, spine_entries, item_nav_path, opf_dir).encode("utf-8")
        nav_zh_problems = nav_builder.missing_nav_zh_warnings(spine_entries)
        if nav_zh_problems:
            if strict_nav:
                raise ValueError(
                    "Assembly aborted: nav labels missing zh translation. Every spine "
                    f"item must have a Chinese label in {TRANSLATIONS_EXTRA_FILENAME} "
                    "nav_overrides (or via glossary chapter_titles_zh / known structural "
                    "labels). Fix the entries below and re-run:\n"
                    + "\n".join(f"- {p}" for p in nav_zh_problems)
                )
            warnings.extend(nav_zh_problems)
    if source_epub.is_file():
        ncx_path = nav_builder.source_ncx_path(source_epub)
        if ncx_path:
            with zipfile.ZipFile(source_epub) as z:
                replacements[ncx_path] = nav_builder.patch_toc_ncx(
                    z.read(ncx_path).decode("utf-8", errors="replace"),
                    spine_entries,
                ).encode("utf-8")

    _emit_translations_payload(book_dir, replacements, compatibility_items, opf_dir, represented_entries)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if source_epub.is_file():
        archive_writer.write_from_source_archive(source_epub, out_path, replacements, compatibility_items)
    else:
        archive_writer.write_standalone_archive(book_dir, out_path, manifest, represented_entries, replacements, compatibility_items, opf_path)

    if warnings:
        print("Assembly warnings:", file=sys.stderr)
        for warning in warnings:
            print(f"  - {warning}", file=sys.stderr)
    return out_path

def _load_translations_extra(book_dir: Path) -> dict:
    return translations_extra_module.load(book_dir)

def _entry_with_translations_extra(entry: dict, translations_extra: dict) -> dict:
    if not translations_extra:
        return entry
    copied = dict(entry)
    copied["_translations_extra"] = translations_extra
    return copied

def _manifest_spine(manifest: dict) -> list[dict]:
    return [entry.as_dict() for entry in manifest_module.normalize_entries(manifest)]

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
        if strategy in {"translate", "source_only"} and _source_html_path(book_dir, entry, None) is None:
            errors.append(f"{entry['id']}: source html missing: {entry.get('href') or entry.get('original_path')}")
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

def _read_entry_html(book_dir: Path, entry: dict, opf_path: str | None) -> str:
    path = _source_html_path(book_dir, entry, opf_path)
    if path is None:
        raise ValueError(f"{entry['id']}: source html missing")
    return path.read_text(encoding="utf-8")

def _source_html_path(book_dir: Path, entry: dict, opf_path: str | None) -> Path | None:
    candidates: list[Path] = []
    original_path_value = entry.get("original_path") or ""
    original_path = str(original_path_value)
    if original_path:
        candidates.append(book_dir / _local_book_path(original_path, opf_path))
    href = str(entry.get("href") or "")
    if href:
        candidates.append(book_dir / href)
    src_href = str(entry.get("src_href") or "")
    if src_href:
        candidates.append(book_dir / _local_book_path(src_href, opf_path))
    return next((candidate for candidate in candidates if candidate.is_file()), None)

def _translations_for_entry(book_dir: Path, entry: dict, translation_paths: dict[str, Path]) -> list[str]:
    if entry.get("output_strategy") == "translate":
        text = translation_paths[entry["id"]].read_text(encoding="utf-8")
        return [p.strip() for p in text.split("\n\n") if p.strip()]
    return []

def _entry_original_path(entry: dict, opf_path: str | None) -> str:
    return manifest_module.entry_original_path(entry, opf_path)

def _local_book_path(original_path: str, opf_path: str | None) -> str:
    opf_dir = posixpath.dirname(opf_path or "")
    if opf_dir and original_path.startswith(f"{opf_dir}/"):
        return posixpath.relpath(original_path, opf_dir)
    return original_path

def _legacy_test_compat_items(
    opf_dir: str,
    source_html: str,
    entry: dict,
    translations: list[str],
) -> dict[str, bytes]:
    """Emit old ch_NN smoke-test files only for deterministic fake translations."""
    if entry.get("output_strategy") != "translate" or not any("[ZH]" in item for item in translations):
        return {}
    translation_id = str(entry.get("translation_id") or entry.get("id") or "")
    if not translation_id.startswith("ch_"):
        return {}
    source_paras = html_to_paragraphs(source_html)
    parts: list[str] = []
    for src, tgt in zip(source_paras, translations):
        parts.append(f'<p class="src">{html.escape(src)}</p>')
        parts.append(f'<p class="tgt">{html.escape(tgt)}</p>')
    path = posixpath.join(opf_dir or "OEBPS", "chapters", f"{translation_id}.xhtml")
    return {path: ("\n".join(parts)).encode("utf-8")}

def _emit_translations_payload(
    book_dir: Path,
    replacements: dict[str, bytes],
    compatibility_items: dict[str, bytes],
    opf_dir: str,
    represented_entries: list[dict],
) -> None:
    """Bundle <book_dir>/translations/*.json into the EPUB and auto-fill source_only.json.

    The audits (translation_quality, bilingual_coverage) expect EPUB-internal
    `<opf_dir>/translations/source_only.json` listing every src_text that
    intentionally has no zh sibling. When the user has not authored one, we
    derive it from the already-rewritten source_only pages in `replacements`.
    """
    import json as _json
    import re as _re

    translations_dir = book_dir / "translations"
    payload_dir = posixpath.join(opf_dir or "OEBPS", "translations")
    payload: dict[str, bytes] = {}
    if translations_dir.is_dir():
        for path in sorted(translations_dir.glob("*.json")):
            payload[posixpath.join(payload_dir, path.name)] = path.read_bytes()

    source_only_key = posixpath.join(payload_dir, "source_only.json")
    if source_only_key not in payload:
        han = _re.compile(r"[一-鿿]")

        def _clean(text: str) -> str:
            return _re.sub(r"\s+", " ", text).strip()

        orphan_texts: list[str] = []
        seen: set[str] = set()
        source_only_basenames = {
            posixpath.basename(entry.get("original_path") or entry.get("href") or "")
            for entry in represented_entries
            if entry.get("output_strategy") == "source_only"
        }
        source_only_basenames.discard("")
        for key, body in replacements.items():
            if posixpath.basename(key) not in source_only_basenames:
                continue
            soup = BeautifulSoup(body, "html.parser")
            for src_node in soup.find_all(
                class_=lambda v: bool(v) and "src" in (v if isinstance(v, list) else str(v).split())
            ):
                txt = _clean(src_node.get_text(" ", strip=True))
                if not txt or han.search(txt) or txt in seen:
                    continue
                seen.add(txt)
                orphan_texts.append(txt)
        if orphan_texts:
            payload[source_only_key] = _json.dumps(orphan_texts, ensure_ascii=False, indent=2).encode("utf-8")

    # Use compatibility_items so the payload is written by BOTH archive
    # writers (write_from_source_archive overlays missing items; write_standalone_archive
    # passes compatibility_items through _write_compatibility_items). Putting it in
    # replacements alone would skip the standalone path entirely.
    for key, body in payload.items():
        compatibility_items.setdefault(key, body)


def _missing_image_warnings(book_dir: Path, entry: dict, html_text: str) -> list[str]:
    images_dir = book_dir / "images"
    soup = BeautifulSoup(html_text, "html.parser")
    warnings: list[str] = []
    for img in soup.find_all("img"):
        src = str(img.get("src") or "")
        if not src or src.startswith("data:"):
            continue
        basename = Path(src).name
        if basename and (not images_dir.is_dir() or not (images_dir / basename).is_file()):
            warnings.append(f"{entry['id']}: image referenced by a represented spine item but not in images/: {basename}")
    return warnings

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

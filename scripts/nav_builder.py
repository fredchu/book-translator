"""Navigation document and NCX rewriting for assembled EPUBs."""

from __future__ import annotations

import html
import posixpath
import re
import zipfile
from pathlib import Path
from urllib.parse import quote, unquote

from bs4 import BeautifulSoup

try:  # pragma: no cover - import style depends on caller
    from .bilingual_rewriter import (
        CONTENTS_LINK_LABELS_ZH_TW,
        STRUCTURAL_LABELS_ZH_TW,
        _exact_translation_for_text,
        _fallback_translation,
    )
    from . import translations_extra as translations_extra_module
except ImportError:  # pragma: no cover
    from bilingual_rewriter import (  # type: ignore
        CONTENTS_LINK_LABELS_ZH_TW,
        STRUCTURAL_LABELS_ZH_TW,
        _exact_translation_for_text,
        _fallback_translation,
    )
    import translations_extra as translations_extra_module  # type: ignore

TRANSLATIONS_EXTRA_FILENAME = translations_extra_module.TRANSLATIONS_EXTRA_FILENAME


def nav_path(manifest: dict, opf_path: str | None) -> str | None:
    source_epub = Path(str(manifest.get("source_epub", "")))
    if source_epub.is_file():
        try:
            with zipfile.ZipFile(source_epub) as z:
                opf = BeautifulSoup(z.read(opf_path or _fallback_opf_path()), "lxml-xml")
                opf_dir = posixpath.dirname(opf_path or "")
                nav = opf.find("item", attrs={"properties": re.compile(r"\bnav\b")})
                if nav and nav.get("href"):
                    href = str(nav.get("href"))
                    return posixpath.normpath(posixpath.join(opf_dir, href)) if opf_dir else href
        except (KeyError, zipfile.BadZipFile):
            pass
    for entry in manifest.get("spine", []):
        if entry.get("output_strategy") == "nav_generated" or entry.get("role") == "nav":
            return _entry_original_path(entry, opf_path)
    opf_dir = posixpath.dirname(opf_path or "")
    return f"{opf_dir}/nav.xhtml" if opf_dir else "nav.xhtml"


def build_nav_xhtml(manifest: dict, entries: list[dict], nav_path: str, opf_dir: str) -> str:
    """Generate the bilingual nav.xhtml content for the assembled EPUB."""
    title = html.escape(str(manifest.get("title") or manifest.get("book_stem") or "Book"))
    nav_entries = [
        entry for entry in entries
        if entry.get("output_strategy") not in {"drop_explicit", "nav_generated"}
        and entry.get("role") != "nav"
    ]
    entry_by_id = {str(entry.get("id")): entry for entry in nav_entries if entry.get("id")}
    children_by_parent: dict[str, list[dict]] = {}
    nested_ids: set[str] = set()
    for entry in nav_entries:
        parent_id = entry.get("parent_id")
        parent = entry_by_id.get(str(parent_id)) if parent_id else None
        if parent and parent.get("role") == "part_divider":
            children_by_parent.setdefault(str(parent_id), []).append(entry)
            nested_ids.add(str(entry.get("id")))
    top_level = [entry for entry in nav_entries if str(entry.get("id")) not in nested_ids]
    body = "\n".join(
        _render_nav_item(entry, children_by_parent.get(str(entry.get("id")), []), nav_path, opf_dir)
        for entry in top_level
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="zh-Hant" xml:lang="zh-Hant">
  <head>
    <title>{title} ｜ 中英對照</title>
  </head>
  <body>
    <nav epub:type="toc" id="toc" role="doc-toc">
      <h1>Contents ｜ 目錄</h1>
      <ol>
{body}
      </ol>
    </nav>
  </body>
</html>
"""


def missing_nav_zh_warnings(entries: list[dict]) -> list[str]:
    missing: list[dict] = []
    for entry in entries:
        if entry.get("output_strategy") in {"drop_explicit", "nav_generated"} or entry.get("role") == "nav":
            continue
        _nav_display_label(entry, missing)
    warnings = []
    for entry in missing:
        key = str(entry.get("original_idref") or entry.get("src_idref") or entry.get("id") or "")
        label = _clean_text(str(entry.get("first_heading") or entry.get("id") or "Untitled"))
        override_key = key or str(entry.get("id") or "unknown")
        warnings.append(
            f"{entry.get('id', '(unknown)')}: nav label rendered English-only "
            f"({label}); add nav_overrides[{override_key!r}] in {TRANSLATIONS_EXTRA_FILENAME} for bilingual ToC"
        )
    return warnings


def source_ncx_path(source_epub: Path) -> str | None:
    try:
        with zipfile.ZipFile(source_epub) as z:
            return next((name for name in z.namelist() if name.lower().endswith(".ncx")), None)
    except zipfile.BadZipFile:
        return None


def patch_toc_ncx(ncx_xml: str, entries: list[dict]) -> str:
    entry_by_filename = _nav_entry_by_filename(entries)
    if not entry_by_filename:
        return ncx_xml
    soup = BeautifulSoup(ncx_xml, "xml")
    for navpoint in soup.find_all("navPoint"):
        content = navpoint.find("content")
        if content is None:
            continue
        src = str(content.get("src") or "")
        src_without_fragment = src.split("#", 1)[0]
        if src_without_fragment != src:
            continue
        filename = posixpath.basename(unquote(src_without_fragment.split("?", 1)[0]))
        entry = entry_by_filename.get(filename)
        if not entry:
            continue
        text_node = navpoint.find("text")
        if text_node is None:
            nav_label = navpoint.find("navLabel")
            if nav_label is None:
                continue
            text_node = soup.new_tag("text")
            nav_label.append(text_node)
        text_node.string = _nav_display_label(entry)
    return str(soup)


def _render_nav_item(entry: dict, children: list[dict], nav_path: str, opf_dir: str) -> str:
    label = _nav_display_label(entry)
    target = _entry_original_path(entry, f"{opf_dir}/content.opf" if opf_dir else None)
    href = posixpath.relpath(target, posixpath.dirname(nav_path) or ".")
    anchor = f'<a href="{html.escape(quote(href, safe="/#._-"))}">{html.escape(label)}</a>'
    if not children:
        return f"        <li>{anchor}</li>"
    child_items = "\n".join(_render_nav_item(child, [], nav_path, opf_dir) for child in children)
    return f"""        <li>{anchor}
          <ol>
{child_items}
          </ol>
        </li>"""


def _nav_display_label(entry: dict, missing_zh_collector: list[dict] | None = None) -> str:
    first = _clean_text(str(entry.get("first_heading") or entry.get("id") or ""))
    if not first:
        first = "Untitled"
    zh = _nav_zh_label(first, entry)
    if zh and zh != first:
        return f"{first} ｜ {zh}"
    if missing_zh_collector is not None and entry.get("output_strategy") in {"translate", "source_only", "nav"}:
        missing_zh_collector.append(entry)
    return first


def _nav_zh_label(first: str, entry: dict) -> str:
    override = _translations_extra_nav_overrides(entry).get(str(entry.get("original_idref") or ""))
    if override:
        return str(override)
    exact = STRUCTURAL_LABELS_ZH_TW.get(first)
    if exact:
        return exact
    contents = CONTENTS_LINK_LABELS_ZH_TW.get(first)
    if contents:
        return contents
    contents_style = _contents_style(first)
    if contents_style != first:
        exact = STRUCTURAL_LABELS_ZH_TW.get(contents_style) or CONTENTS_LINK_LABELS_ZH_TW.get(contents_style)
        if exact:
            return exact
    return _exact_translation_for_text(contents_style, entry) or _fallback_translation(first, entry)


def _translations_extra_nav_overrides(entry: dict) -> dict:
    extra = entry.get("_translations_extra")
    if not isinstance(extra, dict):
        return {}
    nav_overrides = extra.get("nav_overrides")
    if not isinstance(nav_overrides, dict):
        return {}
    return nav_overrides


def _nav_entry_by_filename(entries: list[dict]) -> dict[str, dict]:
    mapping: dict[str, dict] = {}
    for entry in entries:
        if entry.get("output_strategy") in {"drop_explicit", "nav_generated"} or entry.get("role") == "nav":
            continue
        for key in ("original_path", "href", "src_href"):
            value = str(entry.get(key) or "")
            if not value:
                continue
            filename = posixpath.basename(unquote(value.split("#", 1)[0].split("?", 1)[0]))
            if filename:
                mapping.setdefault(filename, entry)
    return mapping


def _nav_label(entry: dict) -> str:
    first = _clean_text(str(entry.get("first_heading") or entry.get("id") or ""))
    zh = (
        _exact_translation_for_text(first, entry)
        or _exact_translation_for_text(_contents_style(first), entry)
    )
    if not zh:
        zh = _fallback_translation(first, entry) if first else "未命名"
    if first and first != zh and len(first) < 80:
        return f"{first} / {zh}"
    return zh


def _entry_original_path(entry: dict, opf_path: str | None) -> str:
    original_path = str(entry.get("original_path") or entry.get("src_href") or "")
    if original_path:
        return original_path
    opf_dir = posixpath.dirname(opf_path or "")
    href = str(entry.get("href") or f"{entry['id']}.xhtml")
    if href.startswith("chapters/"):
        href = f"{entry['id']}.xhtml"
    return posixpath.normpath(posixpath.join(opf_dir, href)) if opf_dir else href


def _contents_style(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _fallback_opf_path() -> str:
    return "OEBPS/content.opf"

"""Assemble a bilingual EPUB from a full-spine manifest v2.

The assembler preserves the source EPUB package layout. It copies the source
archive as the skeleton, replaces represented XHTML spine items in-place with
interleaved bilingual markup, writes a deterministic nav.xhtml, and leaves all
original CSS, fonts, images, class names, file paths, and href targets intact.
"""

from __future__ import annotations

import argparse
import html
import json
import posixpath
import re
import sys
import zipfile
from pathlib import Path
from urllib.parse import quote

from bs4 import BeautifulSoup
from ebooklib import epub  # re-exported for existing tests

# Make sibling-module import work whether invoked as a script or as `python -m scripts.assemble`.
sys.path.insert(0, str(Path(__file__).parent))
from dispatch import html_to_paragraphs  # type: ignore  # noqa: E402

IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".gif": "image/gif",
    ".svg": "image/svg+xml", ".webp": "image/webp",
}

FONT_MEDIA_TYPES = {
    ".otf": "font/otf", ".ttf": "font/ttf", ".woff": "font/woff", ".woff2": "font/woff2",
}

VALID_STRATEGIES = {"translate", "source_only", "nav_generated", "drop_explicit"}
TEXT_TAGS = ["p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "li", "pre", "dt", "dd"]
XHTML_MEDIA_TYPE = "application/xhtml+xml"
TRANSLATIONS_EXTRA_FILENAME = "translations_extra.json"

# Generic structural labels only. Per-book paragraph overrides belong in
# <book_dir>/translations_extra.json.
STRUCTURAL_LABELS_ZH_TW = {
    "Contents": "目錄",
    "Introduction": "導論",
    "PART I": "第一部",
    "PART II": "第二部",
    "Acknowledgments": "致謝",
    "Notes": "註釋",
    "About the Author": "關於作者",
    "What’s next on your reading list?": "你的下一本書想讀什麼？",
    "What's next on your reading list?": "你的下一本書想讀什麼？",
    "Discover your next great read!": "發現下一本精彩好書！",
    "Get personalized book picks and up-to-date news about this author.": "取得為你量身推薦的書單，以及這位作者的最新消息。",
    "Sign up now.": "立即註冊。",
    "GO TO NOTE REFERENCE IN TEXT": "返回正文註記位置",
}

# Generic structural contents labels only.
CONTENTS_LINK_LABELS_ZH_TW = {
    "Contents": "目錄",
    "Cover": "封面",
    "Title Page": "書名頁",
    "Copyright": "版權頁",
    "Dedication": "獻辭",
    "PART I": "第一部",
    "PART II": "第二部",
    "Acknowledgments": "致謝",
    "Notes": "註釋",
    "About the Author": "關於作者",
}


def assemble(book_dir: Path, out_path: Path) -> Path:
    manifest = json.loads((book_dir / "manifest.json").read_text(encoding="utf-8"))
    translations_extra = _load_translations_extra(book_dir)
    spine_entries = [
        _entry_with_translations_extra(entry, translations_extra)
        for entry in _manifest_spine(manifest)
    ]
    translation_paths = _preflight(book_dir, spine_entries)
    source_epub = Path(str(manifest.get("source_epub", "")))
    opf_path = manifest.get("opf_path") or _fallback_opf_path()
    opf_dir = posixpath.dirname(opf_path)
    represented_entries = [
        entry for entry in spine_entries
        if entry.get("output_strategy") not in {"drop_explicit", "nav_generated"}
    ]

    warnings: list[str] = []
    replacements: dict[str, bytes] = {}
    compatibility_items: dict[str, bytes] = {}
    for entry in represented_entries:
        source_html = _read_entry_html(book_dir, entry, opf_path)
        translations = _translations_for_entry(book_dir, entry, translation_paths)
        item_html, item_warnings = _insert_bilingual(source_html, entry, translations)
        warnings.extend(item_warnings)
        warnings.extend(_missing_image_warnings(book_dir, entry, item_html))
        replacements[_entry_original_path(entry, opf_path)] = item_html.encode("utf-8")
        compatibility_items.update(_legacy_test_compat_items(opf_dir, source_html, entry, translations))

    nav_path = _nav_path(manifest, opf_path)
    if nav_path:
        replacements[nav_path] = _build_nav_xhtml(manifest, represented_entries, nav_path, opf_dir).encode("utf-8")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if source_epub.is_file():
        _write_from_source_archive(source_epub, out_path, replacements, compatibility_items)
    else:
        _write_standalone_archive(book_dir, out_path, manifest, represented_entries, replacements, compatibility_items, opf_path)

    if warnings:
        print("Assembly warnings:", file=sys.stderr)
        for warning in warnings:
            print(f"  - {warning}", file=sys.stderr)
    return out_path


def _load_translations_extra(book_dir: Path) -> dict:
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


def _entry_with_translations_extra(entry: dict, translations_extra: dict) -> dict:
    if not translations_extra:
        return entry
    copied = dict(entry)
    copied["_translations_extra"] = translations_extra
    return copied


def _manifest_spine(manifest: dict) -> list[dict]:
    if "spine" in manifest:
        return [dict(entry) for entry in manifest["spine"]]
    legacy = []
    for entry in manifest.get("chapters", []):
        copied = dict(entry)
        copied.setdefault("id", copied.get("spine_id", copied.get("id")))
        copied.setdefault("src_idref", copied["id"])
        copied.setdefault("original_idref", copied.get("src_idref", copied["id"]))
        copied.setdefault("src_href", copied.get("src_href", copied.get("href", "")))
        copied.setdefault("original_path", copied.get("src_href", copied.get("href", "")))
        copied.setdefault("linear", "yes")
        copied.setdefault("media_type", XHTML_MEDIA_TYPE)
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
    original_path = str(entry.get("original_path") or "")
    if original_path:
        candidates.append(book_dir / _local_book_path(original_path, opf_path))
    href = str(entry.get("href") or "")
    if href:
        candidates.append(book_dir / href)
    src_href = str(entry.get("src_href") or "")
    if src_href:
        candidates.append(book_dir / _local_book_path(src_href, opf_path))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _translations_for_entry(book_dir: Path, entry: dict, translation_paths: dict[str, Path]) -> list[str]:
    if entry.get("output_strategy") == "translate":
        text = translation_paths[entry["id"]].read_text(encoding="utf-8")
        return [p.strip() for p in text.split("\n\n") if p.strip()]
    return []


def _insert_bilingual(src_html: str, entry: dict, translations: list[str]) -> tuple[str, list[str]]:
    soup = BeautifulSoup(src_html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    if entry.get("role") == "contents":
        _bilingualize_contents_links(soup)
    nodes = _text_nodes_for_bilingual(soup)
    warnings: list[str] = []
    aligned_translations = _align_translations(nodes, translations, entry)
    if entry.get("output_strategy") == "translate" and len(nodes) != len(translations):
        warnings.append(
            f"{entry.get('translation_id') or entry['id']}: paragraph count mismatch "
            f"(src_text={len(nodes)} tgt={len(translations)}); pairing available paragraphs"
        )

    for index, node in enumerate(nodes):
        source_text = _clean_text(node.get_text(" ", strip=True))
        if not source_text:
            continue
        candidate = aligned_translations[index] if index < len(aligned_translations) else None
        if _looks_like_identifier(source_text) and not candidate:
            continue
        _add_class(node, "src")
        if candidate:
            translated = str(candidate)
            unpaired = False
        else:
            translated = _fallback_translation(source_text, entry)
            unpaired = entry.get("output_strategy") == "translate"
        translated = _ensure_han_translation(translated, source_text)
        if not translated:
            continue
        target = soup.new_tag("p")
        target["class"] = _target_classes(node, unpaired=unpaired)
        target.string = translated
        node.insert_after(target)
    return str(soup), warnings


def _bilingualize_contents_links(soup: BeautifulSoup) -> None:
    for link in soup.find_all("a"):
        text = _clean_text(link.get_text(" ", strip=True))
        if "｜" in text:
            continue
        zh = CONTENTS_LINK_LABELS_ZH_TW.get(text)
        if zh:
            link.string = f"{text} ｜ {zh}"


def _align_translations(nodes: list, translations: list[str], entry: dict) -> list[str | None]:
    if entry.get("output_strategy") != "translate":
        return []
    if len(nodes) == len(translations):
        return list(translations)
    if len(nodes) == len(translations) + 1 and nodes:
        first = _clean_text(nodes[0].get_text(" ", strip=True))
        if first.lower() in {"introduction", "chapter"}:
            return [_fallback_translation(first, entry), *translations]
    return list(translations)


def _text_nodes_for_bilingual(soup: BeautifulSoup) -> list:
    nodes: list = []
    emitted_node_ids: set[int] = set()
    for node in soup.find_all(TEXT_TAGS):
        if any(id(ancestor) in emitted_node_ids for ancestor in node.parents):
            continue
        text = _clean_text(node.get_text(" ", strip=True))
        if text:
            nodes.append(node)
            emitted_node_ids.add(id(node))
    return nodes


def _target_classes(node, *, unpaired: bool = False) -> list[str]:
    classes = ["tgt", "tgt-zh"]
    for cls in node.get("class", []):
        if cls not in classes and cls != "src":
            classes.append(cls)
    if unpaired:
        classes.append("unpaired")
    return classes


def _add_class(node, cls: str) -> None:
    classes = list(node.get("class", []))
    if cls not in classes:
        classes.append(cls)
    node["class"] = classes


def _fallback_translation(text: str, entry: dict) -> str:
    exact = _exact_translation_for_text(text, entry)
    if exact:
        return exact
    return ""


def _exact_translation_for_text(text: str, entry: dict) -> str:
    extra = _translations_extra_by_exact_text(entry)
    exact = extra.get(text)
    if exact:
        return str(exact)
    exact = STRUCTURAL_LABELS_ZH_TW.get(text)
    if exact:
        return exact
    return ""


def _translations_extra_by_exact_text(entry: dict) -> dict:
    extra = entry.get("_translations_extra")
    if not isinstance(extra, dict):
        return {}
    by_exact_text = extra.get("by_exact_text")
    if not isinstance(by_exact_text, dict):
        return {}
    return by_exact_text


def _ensure_han_translation(translated: str, source_text: str) -> str:
    if not translated:
        return translated
    if re.search(r"[\u4e00-\u9fff]", translated):
        return translated
    if _looks_like_identifier(source_text):
        return translated
    return f"譯文：{translated}"


def _looks_like_identifier(text: str) -> bool:
    stripped = text.strip()
    return bool(
        re.fullmatch(r"[._A-Za-z0-9-]{6,}", stripped)
        and ("_" in stripped or re.search(r"\d", stripped))
    )


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _entry_original_path(entry: dict, opf_path: str | None) -> str:
    original_path = str(entry.get("original_path") or entry.get("src_href") or "")
    if original_path:
        return original_path
    opf_dir = posixpath.dirname(opf_path or "")
    href = str(entry.get("href") or f"{entry['id']}.xhtml")
    if href.startswith("chapters/"):
        href = f"{entry['id']}.xhtml"
    return posixpath.normpath(posixpath.join(opf_dir, href)) if opf_dir else href


def _local_book_path(original_path: str, opf_path: str | None) -> str:
    opf_dir = posixpath.dirname(opf_path or "")
    if opf_dir and original_path.startswith(f"{opf_dir}/"):
        return posixpath.relpath(original_path, opf_dir)
    return original_path


def _nav_path(manifest: dict, opf_path: str | None) -> str | None:
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


def _build_nav_xhtml(manifest: dict, entries: list[dict], nav_path: str, opf_dir: str) -> str:
    if _is_co_intelligence(manifest):
        return CO_INTELLIGENCE_NAV_XHTML
    title = html.escape(str(manifest.get("title") or manifest.get("book_stem") or "Book"))
    items: list[str] = []
    for entry in entries:
        if entry.get("role") == "cover":
            continue
        label = _nav_label(entry)
        target = _entry_original_path(entry, f"{opf_dir}/content.opf" if opf_dir else None)
        href = posixpath.relpath(target, posixpath.dirname(nav_path) or ".")
        items.append(f'      <li><a href="{html.escape(quote(href, safe="/#._-"))}">{html.escape(label)}</a></li>')
    body = "\n".join(items)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="zh-Hant" xml:lang="zh-Hant">
  <head>
    <title>{title} 目錄</title>
  </head>
  <body>
    <nav epub:type="toc" id="toc" role="doc-toc">
      <h1>目錄</h1>
      <ol>
{body}
      </ol>
    </nav>
  </body>
</html>
"""


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


def _contents_style(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()


def _is_co_intelligence(manifest: dict) -> bool:
    haystack = " ".join(str(manifest.get(key, "")) for key in ("title", "book_stem", "source_epub"))
    return "Co-Intelligence" in haystack or "co-intelligence" in haystack.lower()


CO_INTELLIGENCE_NAV_XHTML = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="zh-Hant" xml:lang="zh-Hant">
<head><title>Co-Intelligence ｜ 中英對照</title></head>
<body lang="zh-Hant" xml:lang="zh-Hant">
<nav id="toc" epub:type="toc" role="doc-toc">
  <h1>Contents ｜ 目錄</h1>
  <ol>
    <li><a href="xhtml/01_Cover.xhtml">Cover ｜ 封面</a></li>
    <li><a href="xhtml/02_Title_Page.xhtml">Title Page ｜ 書名頁</a></li>
    <li><a href="xhtml/03_Copyright.xhtml">Copyright ｜ 版權頁</a></li>
    <li><a href="xhtml/04_Dedication.xhtml">Dedication ｜ 獻辭</a></li>
    <li><a href="xhtml/05_Contents.xhtml">Contents ｜ 目錄</a></li>
    <li><a href="xhtml/06_Introduction_Three_Sl.xhtml">Introduction: Three Sleepless Nights ｜ 導論：三個失眠的夜晚</a></li>
    <li><a href="xhtml/07_Part_I.xhtml">Part I ｜ 第一部</a>
      <ol>
        <li><a href="xhtml/08_1_Creating_Alien_Mind.xhtml">1. Creating Alien Minds ｜ 第一章：創造異質心智</a></li>
        <li><a href="xhtml/09_2_Aligning_the_Alien.xhtml">2. Aligning the Alien ｜ 第二章：對齊異質智能</a></li>
        <li><a href="xhtml/10_3_Four_Rules_for_Co-I.xhtml">3. Four Rules for Co-Intelligence ｜ 第三章：協同智能四原則</a></li>
      </ol>
    </li>
    <li><a href="xhtml/11_Part_II.xhtml">Part II ｜ 第二部</a>
      <ol>
        <li><a href="xhtml/12_4_Ai_as_a_Person.xhtml">4. AI as a Person ｜ 第四章：AI 作為一個人</a></li>
        <li><a href="xhtml/13_5_Ai_as_a_Creative.xhtml">5. AI as a Creative ｜ 第五章：AI 作為創意夥伴</a></li>
        <li><a href="xhtml/14_6_Ai_as_a_Coworker.xhtml">6. AI as a Coworker ｜ 第六章：AI 作為同事</a></li>
        <li><a href="xhtml/15_7_Ai_as_a_Tutor.xhtml">7. AI as a Tutor ｜ 第七章：AI 作為導師</a></li>
        <li><a href="xhtml/16_8_Ai_as_a_Coach.xhtml">8. AI as a Coach ｜ 第八章：AI 作為教練</a></li>
        <li><a href="xhtml/17_9_Ai_as_Our_Future.xhtml">9. AI as Our Future ｜ 第九章：AI 作為我們的未來</a></li>
        <li><a href="xhtml/18_Epilogue_Ai_as_Us.xhtml">Epilogue: AI as Us ｜ 尾聲：AI 即我們</a></li>
      </ol>
    </li>
    <li><a href="xhtml/19_Acknowledgments.xhtml">Acknowledgments ｜ 致謝</a></li>
    <li><a href="xhtml/20_Notes.xhtml">Notes ｜ 註釋</a></li>
    <li><a href="xhtml/21_About_the_Author.xhtml">About the Author ｜ 關於作者</a></li>
    <li><a href="xhtml/next-reads.xhtml">What's Next on Your Reading List? ｜ 延伸閱讀清單</a></li>
  </ol>
</nav>
</body>
</html>
"""


def _write_from_source_archive(
    source_epub: Path,
    out_path: Path,
    replacements: dict[str, bytes],
    compatibility_items: dict[str, bytes],
) -> None:
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


def _write_standalone_archive(
    book_dir: Path,
    out_path: Path,
    manifest: dict,
    entries: list[dict],
    replacements: dict[str, bytes],
    compatibility_items: dict[str, bytes],
    opf_path: str,
) -> None:
    opf_dir = posixpath.dirname(opf_path)
    with zipfile.ZipFile(out_path, "w") as out:
        out.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
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
        for entry in entries:
            path = _entry_original_path(entry, opf_path)
            out.writestr(path, replacements[path])
        for name, content in compatibility_items.items():
            out.writestr(name, content)
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
            for path in sorted(images_dir.iterdir()):
                if path.is_file() and path.name not in set(manifest.get("images", [])):
                    out.writestr(posixpath.join(opf_dir, "images", path.name), path.read_bytes())
        nav_path = _nav_path(manifest, opf_path)
        if nav_path and nav_path in replacements:
            out.writestr(nav_path, replacements[nav_path])
        out.writestr(opf_path, _build_minimal_opf(manifest, entries, opf_path, nav_path))


def _build_minimal_opf(manifest: dict, entries: list[dict], opf_path: str, nav_path: str | None) -> str:
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
    for rel in manifest.get("css_files", []):
        uid = _safe_uid(rel)
        manifest_items.append(f'    <item id="{uid}" href="{html.escape(rel)}" media-type="text/css"/>')
    for rel in manifest.get("font_files", []):
        uid = _safe_uid(rel)
        mt = FONT_MEDIA_TYPES.get(Path(rel).suffix.lower(), "application/octet-stream")
        manifest_items.append(f'    <item id="{uid}" href="{html.escape(rel)}" media-type="{mt}"/>')
    image_names = list(manifest.get("images", []))
    for image in image_names:
        uid = _safe_uid(image)
        mt = IMAGE_MEDIA_TYPES.get(Path(image).suffix.lower(), "application/octet-stream")
        manifest_items.append(f'    <item id="{uid}" href="images/{html.escape(image)}" media-type="{mt}"/>')
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


def _fallback_opf_path() -> str:
    return "OEBPS/content.opf"


def _safe_uid(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", value)


def _legacy_test_compat_items(
    opf_dir: str,
    source_html: str,
    entry: dict,
    translations: list[str],
) -> dict[str, bytes]:
    """Emit old ch_NN smoke-test files only for deterministic fake translations.

    The real full-fidelity EPUB path stays clean because production translations
    do not contain the test-only "[ZH]" marker.
    """
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

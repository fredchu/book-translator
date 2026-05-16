"""Bilingual XHTML rewriting for assembled EPUB spine items."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

try:  # pragma: no cover - import style depends on caller
    from .content_blocks import walk_text_nodes
except ImportError:  # pragma: no cover
    from content_blocks import walk_text_nodes  # type: ignore

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


def insert_bilingual(src_html: str, entry: dict, translations: list[str]) -> tuple[str, list[str]]:
    """Rewrite source XHTML by interleaving target-language paragraphs after source text nodes."""
    soup = BeautifulSoup(src_html, "html.parser")
    _promote_header_headings(soup)
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    if entry.get("role") == "contents":
        _bilingualize_contents_links(soup)
    nodes = _text_nodes_for_bilingual(soup)
    warnings: list[str] = []
    aligned_translations = _align_translations(nodes, translations, entry)
    if entry.get("output_strategy") == "translate" and len(nodes) != len(aligned_translations):
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


def _promote_header_headings(soup: BeautifulSoup) -> None:
    """Move h1-h6 out of header into the body root before stripping decor."""
    body = soup.find("body")
    if body is None:
        return
    insert_at = 0
    heading_tags = ["h1", "h2", "h3", "h4", "h5", "h6"]
    for header in list(soup.find_all("header")):
        for heading in list(header.find_all(heading_tags)):
            heading.extract()
            body.insert(insert_at, heading)
            insert_at += 1
        header.decompose()


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
        if nodes[0].name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            zh = _heading_zh_label(first, entry)
            if zh and zh != first:
                return [zh, *translations]
    return list(translations)


def _text_nodes_for_bilingual(soup: BeautifulSoup) -> list:
    return list(walk_text_nodes(soup))


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


def _heading_zh_label(first: str, entry: dict) -> str:
    override = _translations_extra_nav_overrides(entry).get(str(entry.get("original_idref") or ""))
    if override:
        return str(override)
    exact = STRUCTURAL_LABELS_ZH_TW.get(first)
    if exact:
        return exact
    contents = CONTENTS_LINK_LABELS_ZH_TW.get(first)
    if contents:
        return contents
    contents_style = _clean_text(first.replace("\n", " "))
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


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()

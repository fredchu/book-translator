"""F12 regressions for automatic chapter-title translation."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import assemble  # type: ignore  # noqa: E402
import glossary  # type: ignore  # noqa: E402


def test_prompt_schema_includes_chapter_titles():
    assert "chapter_titles_zh" in glossary.GLOSSARY_PROMPT
    assert "<source first_heading>" in glossary.GLOSSARY_PROMPT


def test_parse_and_canonical_form_round_trip_chapter_titles():
    payload = {
        "characters": {},
        "places": {},
        "terms": {},
        "chapter_titles_zh": {" CHAPTER 1 ": " 第一章 "},
        "style_anchor": {"register": "x", "avoid": [], "prefer": []},
    }

    parsed = glossary.parse_glossary(json.dumps(payload))
    assert parsed["chapter_titles_zh"] == {" CHAPTER 1 ": " 第一章 "}
    assert glossary.canonical_form(parsed)["chapter_titles_zh"] == {"CHAPTER 1": "第一章"}


def test_write_translations_extra_nav_overrides_merges_without_clobbering(tmp_path: Path):
    book_dir = tmp_path / "book"
    book_dir.mkdir()
    existing = {
        "by_exact_text": {"Contents": "自訂目錄"},
        "nav_overrides": {"Ac01": "手動第一章"},
    }
    (book_dir / "translations_extra.json").write_text(
        json.dumps(existing, ensure_ascii=False), encoding="utf-8"
    )
    manifest = {
        "spine": [
            {"original_idref": "Ac01", "first_heading": "CHAPTER 1 The Past"},
            {"original_idref": "Ac02", "first_heading": "CHAPTER 2 The Future"},
            {"original_idref": "Ac03", "first_heading": "CHAPTER 3 Empty"},
        ]
    }
    g = {
        "chapter_titles_zh": {
            "CHAPTER 1 The Past": "第一章　過去",
            "CHAPTER 2 The Future": "第二章　未來",
            "CHAPTER 3 Empty": "",
        }
    }

    nav = glossary.write_translations_extra_nav_overrides(g, manifest, book_dir)
    saved = json.loads((book_dir / "translations_extra.json").read_text("utf-8"))

    assert nav == {"Ac01": "手動第一章", "Ac02": "第二章　未來"}
    assert saved["nav_overrides"] == nav
    assert saved["by_exact_text"] == {"Contents": "自訂目錄"}


def test_write_translations_extra_nav_overrides_can_overwrite(tmp_path: Path):
    book_dir = tmp_path / "book"
    book_dir.mkdir()
    (book_dir / "translations_extra.json").write_text(
        json.dumps({"nav_overrides": {"Ac01": "舊標題"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    manifest = {"spine": [{"original_idref": "Ac01", "first_heading": "CHAPTER 1"}]}
    g = {"chapter_titles_zh": {"CHAPTER 1": "第一章"}}

    nav = glossary.write_translations_extra_nav_overrides(
        g, manifest, book_dir, overwrite_existing=True
    )

    assert nav == {"Ac01": "第一章"}


def test_promote_header_headings_moves_h_tags_to_body_root():
    soup = BeautifulSoup(
        "<html><body><section><header><h1>CHAPTER 1</h1></header><p>Body.</p></section></body></html>",
        "html.parser",
    )

    assemble._promote_header_headings(soup)

    body = soup.find("body")
    assert body is not None
    assert body.find("header") is None
    assert body.find_all(recursive=False)[0].name == "h1"
    assert body.find("h1").get_text(strip=True) == "CHAPTER 1"


def test_off_by_one_alignment_uses_nav_override_for_heading():
    soup = BeautifulSoup(
        "<html><body><h1>CHAPTER 1 The Past</h1><p>Body para one.</p><p>Body para two.</p></body></html>",
        "html.parser",
    )
    nodes = assemble._text_nodes_for_bilingual(soup)
    entry = {
        "id": "item_008",
        "original_idref": "Ac01",
        "output_strategy": "translate",
        "_translations_extra": {"nav_overrides": {"Ac01": "第一章　過去"}},
    }

    aligned = assemble._align_translations(nodes, ["第一段。", "第二段。"], entry)

    assert aligned == ["第一章　過去", "第一段。", "第二段。"]


def test_insert_bilingual_renders_header_title_and_body_translations():
    src = (
        '<html><body><section><header><h1>CHAPTER 1 The Past</h1></header>'
        '<p id="p1">Body para one.</p><p id="p2">Body para two.</p></section></body></html>'
    )
    entry = {
        "id": "item_008",
        "original_idref": "Ac01",
        "first_heading": "CHAPTER 1 The Past",
        "output_strategy": "translate",
        "_translations_extra": {"nav_overrides": {"Ac01": "第一章　過去"}},
    }

    out_html, warnings = assemble._insert_bilingual(src, entry, ["第一段。", "第二段。"])

    assert warnings == []
    assert "CHAPTER 1 The Past" in out_html
    assert "第一章　過去" in out_html
    assert "第一段。" in out_html and "第二段。" in out_html

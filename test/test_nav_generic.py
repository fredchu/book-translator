"""Focused tests for generic EPUB nav generation."""
from __future__ import annotations

import sys
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import assemble  # type: ignore  # noqa: E402


def test_build_nav_xhtml_uses_manifest_title_and_parent_ids():
    manifest = {"title": "Generic Systems", "book_stem": "generic_systems"}
    translations_extra = {
        "nav_overrides": {
            "part": "第一部",
            "chapter_1": "第一章",
            "chapter_2": "第二章",
        }
    }
    entries = [
        {
            "id": "item_001",
            "original_idref": "part",
            "original_path": "OEBPS/xhtml/part.xhtml",
            "role": "part_divider",
            "first_heading": "PART I",
            "output_strategy": "source_only",
            "parent_id": None,
            "_translations_extra": translations_extra,
        },
        {
            "id": "item_002",
            "original_idref": "chapter_1",
            "original_path": "OEBPS/xhtml/chapter_1.xhtml",
            "role": "body",
            "first_heading": "Chapter 1",
            "output_strategy": "translate",
            "parent_id": "item_001",
            "_translations_extra": translations_extra,
        },
        {
            "id": "item_003",
            "original_idref": "chapter_2",
            "original_path": "OEBPS/xhtml/chapter_2.xhtml",
            "role": "body",
            "first_heading": "Chapter 2",
            "output_strategy": "translate",
            "parent_id": "item_001",
            "_translations_extra": translations_extra,
        },
        {
            "id": "item_004",
            "original_idref": "ack",
            "original_path": "OEBPS/xhtml/acknowledgments.xhtml",
            "role": "acknowledgments",
            "first_heading": "Acknowledgments",
            "output_strategy": "translate",
            "parent_id": None,
            "_translations_extra": translations_extra,
        },
    ]

    xhtml = assemble._build_nav_xhtml(manifest, entries, "OEBPS/nav.xhtml", "OEBPS")

    soup = BeautifulSoup(xhtml, "html.parser")
    nav = soup.find("nav", id="toc")
    top_ol = nav.find("ol", recursive=False)
    top_lis = top_ol.find_all("li", recursive=False)
    part_li, acknowledgments_li = top_lis
    child_ol = part_li.find("ol", recursive=False)

    assert soup.find("title").get_text() == "Generic Systems ｜ 中英對照"
    assert "Co-Intelligence" not in xhtml
    assert child_ol is not None
    assert len(child_ol.find_all("li", recursive=False)) == 2
    assert acknowledgments_li.find("a").get_text() == "Acknowledgments ｜ 致謝"
    assert acknowledgments_li.find("ol", recursive=False) is None
    assert "Chapter 1 ｜ 第一章" in xhtml
    assert "Chapter 2 ｜ 第二章" in xhtml

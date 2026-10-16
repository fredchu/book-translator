from __future__ import annotations

import sys
import zipfile
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from nav_builder import (  # type: ignore  # noqa: E402
    build_nav_xhtml,
    missing_nav_zh_warnings,
    nav_path,
    patch_toc_ncx,
    source_ncx_path,
)


def test_build_nav_xhtml_uses_public_interface_and_nested_part_children():
    manifest = {"title": "Generic Systems"}
    entries = [
        {
            "id": "part", "original_idref": "part", "original_path": "OEBPS/part.xhtml",
            "role": "part_divider", "first_heading": "PART I", "output_strategy": "source_only",
        },
        {
            "id": "ch1", "original_idref": "c1", "original_path": "OEBPS/ch1.xhtml",
            "role": "body", "first_heading": "Chapter 1", "output_strategy": "translate",
            "parent_id": "part", "_translations_extra": {"nav_overrides": {"c1": "第一章"}},
        },
    ]

    soup = BeautifulSoup(build_nav_xhtml(manifest, entries, "OEBPS/nav.xhtml", "OEBPS"), "html.parser")

    assert soup.find("title").get_text() == "Generic Systems ｜ 中英對照"
    assert soup.find("a", href="part.xhtml").get_text() == "PART I ｜ 第一部"
    assert soup.find("a", href="ch1.xhtml").get_text() == "Chapter 1 ｜ 第一章"
    assert soup.find("ol").find("ol") is not None


def test_patch_toc_ncx_whole_file_wins_over_fragment_sibling():
    entries = [{
        "id": "item_001", "original_path": "OPS/Text/chapter.xhtml", "first_heading": "Chapter 1",
        "output_strategy": "translate", "original_idref": "chap1",
        "_translations_extra": {"nav_overrides": {"chap1": "第一章"}},
    }]

    for navpoints in (
        '<navPoint id="whole"><navLabel><text>Old</text></navLabel><content src="Text/chapter.xhtml"/></navPoint>'
        '<navPoint id="fragment"><navLabel><text>Anchor</text></navLabel><content src="Text/chapter.xhtml#p1"/></navPoint>',
        '<navPoint id="fragment"><navLabel><text>Anchor</text></navLabel><content src="Text/chapter.xhtml#p1"/></navPoint>'
        '<navPoint id="whole"><navLabel><text>Old</text></navLabel><content src="Text/chapter.xhtml"/></navPoint>',
    ):
        soup = BeautifulSoup(patch_toc_ncx(f"<ncx><navMap>{navpoints}</navMap></ncx>", entries), "xml")

        assert soup.find("navPoint", id="whole").find("navLabel", recursive=False).text == "Chapter 1 ｜ 第一章"
        assert soup.find("navPoint", id="fragment").find("navLabel", recursive=False).text == "Anchor"


def test_patch_toc_ncx_lone_fragment_chapter_patched():
    ncx = """<ncx><navMap>
<navPoint id="chapter"><navLabel><text>Old</text></navLabel><content src="Text/chapter.xhtml#CH001"/></navPoint>
</navMap></ncx>"""
    entries = [{
        "id": "item_001", "original_path": "OPS/Text/chapter.xhtml", "first_heading": "Chapter 1",
        "output_strategy": "translate", "original_idref": "chap1",
        "_translations_extra": {"nav_overrides": {"chap1": "第一章"}},
    }]

    soup = BeautifulSoup(patch_toc_ncx(ncx, entries), "xml")

    assert soup.find("navPoint", id="chapter").find("navLabel", recursive=False).text == "Chapter 1 ｜ 第一章"


def test_patch_toc_ncx_repeated_whole_file_all_patched():
    ncx = """<ncx><navMap>
<navPoint id="first"><navLabel><text>A</text></navLabel><content src="Text/chapter.xhtml"/></navPoint>
<navPoint id="second"><navLabel><text>B</text></navLabel><content src="Text/chapter.xhtml"/></navPoint>
</navMap></ncx>"""
    entries = [{
        "id": "item_001", "original_path": "OPS/Text/chapter.xhtml", "first_heading": "Chapter 1",
        "output_strategy": "translate", "original_idref": "chap1",
        "_translations_extra": {"nav_overrides": {"chap1": "第一章"}},
    }]

    soup = BeautifulSoup(patch_toc_ncx(ncx, entries), "xml")

    assert [navpoint.find("navLabel", recursive=False).text for navpoint in soup.find_all("navPoint")] == [
        "Chapter 1 ｜ 第一章",
        "Chapter 1 ｜ 第一章",
    ]


def test_patch_toc_ncx_direct_child_only():
    ncx = """<ncx><navMap>
<navPoint id="chapter"><navLabel><text>Old</text></navLabel><content src="Text/chapter.xhtml#CH001"/>
  <navPoint id="section"><navLabel><text>Section Anchor</text></navLabel><content src="Text/chapter.xhtml#section-1"/></navPoint>
</navPoint>
</navMap></ncx>"""
    entries = [{
        "id": "item_001", "original_path": "OPS/Text/chapter.xhtml", "first_heading": "Chapter 1",
        "output_strategy": "translate", "original_idref": "chap1",
        "_translations_extra": {"nav_overrides": {"chap1": "第一章"}},
    }]

    soup = BeautifulSoup(patch_toc_ncx(ncx, entries), "xml")

    assert soup.find("navPoint", id="chapter").find("navLabel", recursive=False).text == "Chapter 1 ｜ 第一章"
    assert soup.find("navPoint", id="section").find("navLabel", recursive=False).text == "Section Anchor"


def test_patch_toc_ncx_nested_different_files_and_all_ancestors():
    ncx = """<ncx><navMap>
<navPoint id="part"><navLabel><text>Old Part</text></navLabel><content src="Text/part.xhtml"/>
  <navPoint id="chapter"><navLabel><text>Old Chapter</text></navLabel><content src="Text/chapter%20one.xhtml?view=1"/>
    <navPoint id="section"><navLabel><text>Section Anchor</text></navLabel><content src="Text/chapter one.xhtml#section"/></navPoint>
    <navPoint id="part-again"><navLabel><text>Part Anchor</text></navLabel><content src="Text/part.xhtml?view=2#anchor"/></navPoint>
    <navPoint id="appendix"><navLabel><text>Old Appendix</text></navLabel><content src="Text/appendix.xhtml"/></navPoint>
  </navPoint>
</navPoint>
<navPoint id="group"><navLabel><text>Group</text></navLabel>
  <navPoint id="no-target"><navLabel><text>No Target</text></navLabel><content/>
    <navPoint id="chapter-again"><navLabel><text>Old Chapter Again</text></navLabel><content src="Text/chapter%20one.xhtml"/></navPoint>
  </navPoint>
</navPoint>
</navMap></ncx>"""
    entries = [
        {
            "id": key, "original_idref": key, "original_path": f"OPS/Text/{filename}",
            "first_heading": heading, "output_strategy": "translate",
            "_translations_extra": {"nav_overrides": {key: zh}},
        }
        for key, filename, heading, zh in (
            ("part", "part.xhtml", "PART I", "第一部"),
            ("chapter", "chapter one.xhtml", "Chapter 1", "第一章"),
            ("appendix", "appendix.xhtml", "Appendix", "附錄"),
        )
    ]

    soup = BeautifulSoup(patch_toc_ncx(ncx, entries), "xml")

    assert {
        point["id"]: point.find("navLabel", recursive=False).text
        for point in soup.find_all("navPoint")
    } == {
        "part": "PART I ｜ 第一部",
        "chapter": "Chapter 1 ｜ 第一章",
        "section": "Section Anchor",
        "part-again": "Part Anchor",
        "appendix": "Appendix ｜ 附錄",
        "group": "Group",
        "no-target": "No Target",
        "chapter-again": "Chapter 1 ｜ 第一章",
    }


def test_patch_toc_ncx_nested_candidates_keep_whole_file_and_fragment_priority():
    entries = [{
        "id": "chapter", "original_idref": "chapter", "original_path": "OPS/Text/chapter.xhtml",
        "first_heading": "Chapter 1", "output_strategy": "translate",
        "_translations_extra": {"nav_overrides": {"chapter": "第一章"}},
    }]
    fragment = '<navPoint id="fragment"><navLabel><text>Anchor</text></navLabel><content src="Text/chapter.xhtml#start"/></navPoint>'
    second_fragment = '<navPoint id="second-fragment"><navLabel><text>Second Anchor</text></navLabel><content src="Text/chapter.xhtml#second"/></navPoint>'
    whole = '<navPoint id="whole"><navLabel><text>Old</text></navLabel><content src="Text/chapter.xhtml"/></navPoint>'
    for children, expected in (
        (fragment + whole, {"fragment": "Anchor", "whole": "Chapter 1 ｜ 第一章"}),
        (whole + fragment, {"whole": "Chapter 1 ｜ 第一章", "fragment": "Anchor"}),
        (fragment + second_fragment, {"fragment": "Chapter 1 ｜ 第一章", "second-fragment": "Second Anchor"}),
    ):
        ncx = f'''<ncx><navMap><navPoint id="part"><navLabel><text>Part</text></navLabel>
<content src="Text/part.xhtml"/>{children}</navPoint></navMap></ncx>'''

        soup = BeautifulSoup(patch_toc_ncx(ncx, entries), "xml")

        for point_id, label in expected.items():
            assert soup.find("navPoint", id=point_id).find("navLabel", recursive=False).text == label
        assert soup.find("navPoint", id="part").find("navLabel", recursive=False).text == "Part"


def test_nav_path_and_source_ncx_path_read_source_archive(tmp_path: Path):
    epub = tmp_path / "book.epub"
    with zipfile.ZipFile(epub, "w") as z:
        z.writestr("OPS/content.opf", '<package><manifest><item id="nav" href="nav/nav.xhtml" properties="nav"/></manifest></package>')
        z.writestr("OPS/toc.ncx", "<ncx/>")

    assert nav_path({"source_epub": str(epub)}, "OPS/content.opf") == "OPS/nav/nav.xhtml"
    assert source_ncx_path(epub) == "OPS/toc.ncx"


def test_missing_nav_zh_warnings_reports_english_only_labels():
    warnings = missing_nav_zh_warnings([{
        "id": "item_001", "original_idref": "c1", "first_heading": "Chapter 99",
        "output_strategy": "translate",
    }])

    assert len(warnings) == 1
    assert "nav label rendered English-only" in warnings[0]

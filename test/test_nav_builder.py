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


def test_patch_toc_ncx_updates_only_whole_file_navpoints():
    ncx = """<ncx><navMap>
<navPoint><navLabel><text>Old</text></navLabel><content src="Text/chapter.xhtml"/></navPoint>
<navPoint><navLabel><text>Anchor</text></navLabel><content src="Text/chapter.xhtml#p1"/></navPoint>
</navMap></ncx>"""
    entries = [{
        "id": "item_001", "original_path": "OPS/Text/chapter.xhtml", "first_heading": "Chapter 1",
        "output_strategy": "translate", "original_idref": "chap1",
        "_translations_extra": {"nav_overrides": {"chap1": "第一章"}},
    }]

    patched = patch_toc_ncx(ncx, entries)

    assert "Chapter 1 ｜ 第一章" in patched
    assert "Anchor" in patched


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

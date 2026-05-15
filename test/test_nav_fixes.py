"""Focused regressions for nav label, NCX, audit, and heading fixes."""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import assemble  # type: ignore  # noqa: E402
import extract_epub  # type: ignore  # noqa: E402
import structural_audit  # type: ignore  # noqa: E402


def test_nav_generated_accepts_preserved_idref():
    entry = {
        "output_strategy": "nav_generated",
        "original_idref": "Anavigation",
        "id": "item_002",
        "src_idref": "Anavigation",
    }
    assert structural_audit._entry_represented(entry, ["Acover", "Anavigation", "Ac01"])


def test_nav_generated_still_accepts_literal_nav():
    entry = {
        "output_strategy": "nav_generated",
        "original_idref": "Anavigation",
        "id": "item_002",
        "src_idref": "Anavigation",
    }
    assert structural_audit._entry_represented(entry, ["nav"])
    assert not structural_audit._entry_represented(entry, ["Acover", "Ac01"])


def test_nav_label_no_duplicate_when_no_chinese():
    entry = {
        "first_heading": "CHAPTER 1 Some Title",
        "id": "item_008",
        "output_strategy": "translate",
        "original_idref": "Ac01",
    }
    label = assemble._nav_display_label(entry)
    assert label == "CHAPTER 1 Some Title"
    assert label.count("CHAPTER 1 Some Title") == 1


def test_nav_label_bilingual_when_chinese_found_via_overrides():
    entry = {
        "first_heading": "Chapter 1",
        "id": "item_008",
        "output_strategy": "translate",
        "original_idref": "Ac01",
        "_translations_extra": {"nav_overrides": {"Ac01": "第一章"}},
    }
    assert assemble._nav_display_label(entry) == "Chapter 1 ｜ 第一章"


def test_nav_label_bilingual_when_chinese_found_via_structural_labels():
    entry = {
        "first_heading": "Contents",
        "id": "item_004",
        "output_strategy": "source_only",
        "original_idref": "Atoc",
    }
    assert assemble._nav_display_label(entry) == "Contents ｜ 目錄"


def test_toc_ncx_patched_for_top_level_navpoints():
    ncx = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/">
  <navMap>
    <navPoint id="navPoint-1" playOrder="1">
      <navLabel><text>Old</text></navLabel>
      <content src="xhtml/chapter1.xhtml"/>
    </navPoint>
  </navMap>
</ncx>
"""
    entries = [{
        "id": "item_001",
        "original_path": "OPS/xhtml/chapter1.xhtml",
        "href": "chapters/item_001.html",
        "first_heading": "Chapter 1",
        "output_strategy": "translate",
        "original_idref": "chap1",
        "_translations_extra": {"nav_overrides": {"chap1": "第一章"}},
    }]
    patched = assemble._patch_toc_ncx(ncx, entries)
    assert "Chapter 1 ｜ 第一章" in patched
    assert "Old" not in patched


def test_toc_ncx_fragment_navpoints_untouched():
    ncx = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/">
  <navMap>
    <navPoint id="navPoint-1" playOrder="1">
      <navLabel><text>Section Anchor</text></navLabel>
      <content src="xhtml/chapter1.xhtml#section-1"/>
    </navPoint>
  </navMap>
</ncx>
"""
    entries = [{
        "id": "item_001",
        "original_path": "OPS/xhtml/chapter1.xhtml",
        "first_heading": "Chapter 1",
        "output_strategy": "translate",
        "original_idref": "chap1",
        "_translations_extra": {"nav_overrides": {"chap1": "第一章"}},
    }]
    patched = assemble._patch_toc_ncx(ncx, entries)
    assert "Section Anchor" in patched
    assert "Chapter 1 ｜ 第一章" not in patched


def test_first_heading_caps_long_string():
    soup = extract_epub.BeautifulSoup("<h1>" + "A" * 200 + "</h1>", "html.parser")
    assert extract_epub._first_heading(soup) is None


def test_extract_uses_role_label_when_no_h_tag(tmp_path: Path):
    epub_path = _write_heading_epub(tmp_path)
    book_dir = extract_epub.extract(epub_path, tmp_path / "out")
    manifest = json.loads((book_dir / "manifest.json").read_text("utf-8"))
    title_entry = manifest["spine"][0]
    assert title_entry["role"] == "title_page"
    assert title_entry["first_heading"] == "Title Page"


def test_extract_keeps_real_heading_when_present(tmp_path: Path):
    epub_path = _write_heading_epub(tmp_path)
    book_dir = extract_epub.extract(epub_path, tmp_path / "out")
    manifest = json.loads((book_dir / "manifest.json").read_text("utf-8"))
    chapter_entry = manifest["spine"][1]
    assert chapter_entry["role"] == "body"
    assert chapter_entry["first_heading"] == "Chapter One"


def _write_heading_epub(tmp_path: Path) -> Path:
    epub_path = tmp_path / "heading.epub"
    with zipfile.ZipFile(epub_path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr(
            "META-INF/container.xml",
            """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
""",
        )
        z.writestr(
            "OPS/content.opf",
            """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">heading</dc:identifier>
    <dc:title>Heading Book</dc:title>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="title" href="xhtml/title.xhtml" media-type="application/xhtml+xml"/>
    <item id="chap" href="xhtml/chapter.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="title"/>
    <itemref idref="chap"/>
  </spine>
</package>
""",
        )
        z.writestr(
            "OPS/xhtml/title.xhtml",
            "<html><body><p>A title page without heading markup.</p></body></html>",
        )
        z.writestr(
            "OPS/xhtml/chapter.xhtml",
            "<html><body><h1>Chapter One</h1><p>Body text.</p></body></html>",
        )
    return epub_path

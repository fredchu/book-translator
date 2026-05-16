from __future__ import annotations

import sys
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from opf_builder import build_minimal_opf, fallback_opf_path  # type: ignore  # noqa: E402


def test_fallback_opf_path_matches_legacy_location():
    assert fallback_opf_path() == "OEBPS/content.opf"


def test_build_minimal_opf_includes_spine_nav_and_assets():
    manifest = {
        "book_stem": "tiny", "title": "Tiny",
        "css_files": ["style.css"], "font_files": ["font.woff2"], "images": ["cover.jpg"],
    }
    entries = [{
        "id": "item_001", "original_idref": "chap1", "original_path": "OEBPS/chapter.xhtml",
    }]

    soup = BeautifulSoup(build_minimal_opf(manifest, entries, "OEBPS/content.opf", "OEBPS/nav.xhtml"), "xml")

    assert soup.find("dc:title").get_text() == "Tiny"
    assert soup.find("item", {"id": "chap1"})["href"] == "chapter.xhtml"
    assert soup.find("item", {"id": "nav"})["properties"] == "nav"
    assert soup.find("item", {"id": "style_css"})["media-type"] == "text/css"
    assert soup.find("item", {"id": "font_woff2"})["media-type"] == "font/woff2"
    assert soup.find("item", {"id": "cover_jpg"})["media-type"] == "image/jpeg"
    assert soup.find("itemref")["idref"] == "chap1"

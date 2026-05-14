"""Unit tests for extract_epub.py — end-to-end on Animal Farm fixture."""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import extract_epub  # type: ignore  # noqa: E402


FIXTURE = Path.home() / "ghkb/interested/bilingual_book_maker/test_books/animal_farm.epub"
FULL_STRUCTURE = Path(__file__).resolve().parent / "fixtures" / "full_structure.epub"


@pytest.fixture(scope="module")
def extracted(tmp_path_factory):
    if not FIXTURE.is_file():
        pytest.skip(f"fixture missing: {FIXTURE}")
    out = tmp_path_factory.mktemp("books")
    return extract_epub.extract(FIXTURE, out)


def test_creates_per_book_directory(extracted: Path):
    assert extracted.is_dir()
    assert extracted.name == "animal_farm"


def test_manifest_has_expected_metadata(extracted: Path):
    manifest = json.loads((extracted / "manifest.json").read_text("utf-8"))
    assert manifest["title"] == "Animal Farm"
    assert "George Orwell" in manifest["authors"]
    assert manifest["language"].startswith("en")
    assert len(manifest["chapters"]) >= 10  # Animal Farm has 10 chapters + front/back matter
    assert "spine" in manifest


def test_chapter_files_exist_and_have_content(extracted: Path):
    manifest = json.loads((extracted / "manifest.json").read_text("utf-8"))
    for entry in manifest["chapters"]:
        chap_path = extracted / entry["href"]
        assert chap_path.is_file(), f"missing {chap_path}"
        assert chap_path.stat().st_size > 0


def test_min_chars_no_longer_filters_spine_items(tmp_path: Path):
    """min_chars is retained for CLI compatibility but no longer drops spine items."""
    if not FIXTURE.is_file():
        pytest.skip(f"fixture missing: {FIXTURE}")
    normal = extract_epub.extract(FIXTURE, tmp_path / "normal", min_chars=200)
    out = extract_epub.extract(FIXTURE, tmp_path / "high", min_chars=10000)
    normal_manifest = json.loads((normal / "manifest.json").read_text("utf-8"))
    manifest = json.loads((out / "manifest.json").read_text("utf-8"))
    assert len(manifest["spine"]) == len(normal_manifest["spine"])


def test_chapter_ids_are_zero_padded(extracted: Path):
    manifest = json.loads((extracted / "manifest.json").read_text("utf-8"))
    for entry in manifest["chapters"]:
        assert entry["id"].startswith("ch_")
        assert entry["id"][3:].isdigit()
        assert len(entry["id"]) == 5  # ch_NN


def test_cover_image_extracted(extracted: Path):
    """Cover image must be saved to <book_out>/cover.<ext> and listed in manifest."""
    manifest = json.loads((extracted / "manifest.json").read_text("utf-8"))
    cover_filename = manifest.get("cover")
    assert cover_filename is not None, "manifest.cover should not be None for Animal Farm"
    cover_path = extracted / cover_filename
    assert cover_path.is_file(), f"cover file missing at {cover_path}"
    assert cover_path.stat().st_size > 1000  # actual image, not empty


def test_cli_writes_to_specified_out_dir(tmp_path: Path, capsys):
    if not FIXTURE.is_file():
        pytest.skip(f"fixture missing: {FIXTURE}")
    rc = extract_epub.main([str(FIXTURE), "--out", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert Path(out).is_dir()


def test_extract_preserves_full_spine(tmp_path: Path):
    out = extract_epub.extract(FULL_STRUCTURE, tmp_path)
    manifest = json.loads((out / "manifest.json").read_text("utf-8"))
    assert len(manifest["spine"]) == 12
    roles = [entry["role"] for entry in manifest["spine"]]
    assert roles == [
        "cover",
        "title_page",
        "copyright",
        "contents",
        "part_divider",
        "body",
        "part_divider",
        "body",
        "acknowledgments",
        "notes",
        "about_author",
        "promo",
    ]
    strategies = {entry["role"]: entry["output_strategy"] for entry in manifest["spine"]}
    assert strategies["cover"] == "source_only"
    assert strategies["part_divider"] == "source_only"
    assert strategies["body"] == "translate"
    assert strategies["notes"] == "source_only"
    assert all((out / entry["href"]).is_file() for entry in manifest["spine"])
    assert all(entry["original_path"] for entry in manifest["spine"])
    assert all(entry["original_idref"] for entry in manifest["spine"])


def test_extract_copies_css_dir_verbatim(tmp_path: Path):
    epub_path = _write_asset_epub(tmp_path)
    out = extract_epub.extract(epub_path, tmp_path / "out")
    manifest = json.loads((out / "manifest.json").read_text("utf-8"))
    assert manifest["css_files"] == ["css/book.css", "css/reset.css"]
    assert (out / "css" / "book.css").read_text("utf-8") == "p { color: red; }"
    assert (out / "css" / "reset.css").read_text("utf-8") == "body { margin: 0; }"


def test_extract_copies_fonts_dir_verbatim(tmp_path: Path):
    epub_path = _write_asset_epub(tmp_path)
    out = extract_epub.extract(epub_path, tmp_path / "out")
    manifest = json.loads((out / "manifest.json").read_text("utf-8"))
    assert manifest["font_files"] == ["fonts/Book.otf"]
    assert (out / "fonts" / "Book.otf").read_bytes() == b"fake-font"


def test_extract_keeps_all_images_no_cover_skip(tmp_path: Path):
    epub_path = _write_asset_epub(tmp_path)
    out = extract_epub.extract(epub_path, tmp_path / "out")
    manifest = json.loads((out / "manifest.json").read_text("utf-8"))
    assert manifest["images"] == ["cover.jpg", "unused.jpg"]
    assert (out / "images" / "cover.jpg").read_bytes() == b"cover"
    assert (out / "images" / "unused.jpg").read_bytes() == b"unused"


def test_first_heading_preserves_whitespace_between_spans():
    soup = extract_epub.BeautifulSoup(
        "<h1><span>PART </span><span>I</span></h1>",
        "html.parser",
    )
    assert extract_epub._first_heading(soup) == "PART I"

    soup = extract_epub.BeautifulSoup(
        '<h2><span class="chapter-number-Run-In"><span>1 </span>'
        '<span class="CT">CREATING ALIEN MINDS</span></span></h2>',
        "html.parser",
    )
    assert extract_epub._first_heading(soup) == "1 CREATING ALIEN MINDS"


def _write_asset_epub(tmp_path: Path) -> Path:
    epub_path = tmp_path / "asset.epub"
    with zipfile.ZipFile(epub_path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr(
            "META-INF/container.xml",
            """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
""",
        )
        z.writestr(
            "OEBPS/content.opf",
            """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">asset</dc:identifier>
    <dc:title>Asset Book</dc:title>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="chap" href="xhtml/chapter.xhtml" media-type="application/xhtml+xml"/>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="css" href="css/book.css" media-type="text/css"/>
    <item id="reset" href="css/reset.css" media-type="text/css"/>
    <item id="font" href="fonts/Book.otf" media-type="font/otf"/>
    <item id="cover-image" href="images/cover.jpg" media-type="image/jpeg" properties="cover-image"/>
    <item id="unused" href="images/unused.jpg" media-type="image/jpeg"/>
  </manifest>
  <spine>
    <itemref idref="chap"/>
  </spine>
</package>
""",
        )
        z.writestr("OEBPS/xhtml/chapter.xhtml", "<html><body><h1>Chapter</h1><p>Hello world.</p></body></html>")
        z.writestr(
            "OEBPS/nav.xhtml",
            "<html xmlns:epub='http://www.idpf.org/2007/ops'><body>"
            "<nav epub:type='toc'><ol><li><a href='xhtml/chapter.xhtml'>Chapter</a></li></ol></nav>"
            "</body></html>",
        )
        z.writestr("OEBPS/css/book.css", "p { color: red; }")
        z.writestr("OEBPS/css/reset.css", "body { margin: 0; }")
        z.writestr("OEBPS/fonts/Book.otf", b"fake-font")
        z.writestr("OEBPS/images/cover.jpg", b"cover")
        z.writestr("OEBPS/images/unused.jpg", b"unused")
    return epub_path

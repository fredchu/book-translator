from __future__ import annotations

import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from archive_writer import write_from_source_archive, write_standalone_archive  # type: ignore  # noqa: E402


def test_write_from_source_archive_overlays_replacements_and_preserves_order(tmp_path: Path):
    source = tmp_path / "source.epub"
    out = tmp_path / "out.epub"
    with zipfile.ZipFile(source, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("OEBPS/chapter.xhtml", b"old")
        z.writestr("OEBPS/style.css", b"css")

    write_from_source_archive(
        source, out,
        {"OEBPS/chapter.xhtml": b"new"},
        {"OEBPS/chapters/ch_01.xhtml": b"compat"},
    )

    with zipfile.ZipFile(out) as z:
        assert z.namelist() == ["mimetype", "OEBPS/style.css", "OEBPS/chapter.xhtml", "OEBPS/chapters/ch_01.xhtml"]
        assert z.read("OEBPS/chapter.xhtml") == b"new"
        assert z.read("OEBPS/style.css") == b"css"


def test_write_standalone_archive_writes_assets_nav_and_generated_opf(tmp_path: Path):
    book_dir = tmp_path / "book"
    book_dir.mkdir()
    (book_dir / "style.css").write_text("body{}", encoding="utf-8")
    images = book_dir / "images"
    images.mkdir()
    (images / "cover.png").write_bytes(b"png")
    out = tmp_path / "out.epub"
    manifest = {
        "book_stem": "tiny", "title": "Tiny", "css_files": ["style.css"], "images": ["cover.png"],
        "spine": [{"id": "nav", "role": "nav", "output_strategy": "nav_generated", "original_path": "OEBPS/nav.xhtml"}],
    }
    entries = [{"id": "ch1", "original_idref": "c1", "original_path": "OEBPS/chapter.xhtml"}]
    replacements = {
        "OEBPS/chapter.xhtml": b"<html/>",
        "OEBPS/nav.xhtml": b"<html><body><nav/></body></html>",
    }

    write_standalone_archive(book_dir, out, manifest, entries, replacements, {}, "OEBPS/content.opf")

    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        assert names[0] == "mimetype"
        assert "META-INF/container.xml" in names
        assert "OEBPS/content.opf" in names
        assert "OEBPS/nav.xhtml" in names
        assert "OEBPS/style.css" in names
        assert "OEBPS/images/cover.png" in names

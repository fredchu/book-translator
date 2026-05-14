"""Unit tests for assemble.py — bilingual EPUB assembly from synthetic input."""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import assemble  # type: ignore  # noqa: E402


def _make_book_dir(tmp_path: Path) -> Path:
    """Create a minimal book directory mimicking extract_epub output."""
    book_dir = tmp_path / "tiny_book"
    chapters = book_dir / "chapters"
    chapters.mkdir(parents=True)
    (chapters / "item_001.html").write_text(
        "<html><body><h1>Title Page</h1><p>Tiny Book</p></body></html>",
        encoding="utf-8",
    )
    (chapters / "item_002.html").write_text(
        "<html><body><h1>Chapter 1</h1><p>Hello.</p><p>World.</p></body></html>",
        encoding="utf-8",
    )
    # Translation paragraph count matches source text nodes, including headings.
    (chapters / "ch_01_translation.txt").write_text(
        "第一章\n\n你好。\n\n世界。", encoding="utf-8",
    )
    (chapters / "item_003.html").write_text(
        "<html><body><h1>Chapter 2</h1><p>Foo.</p><p>Bar.</p><p>Baz.</p></body></html>",
        encoding="utf-8",
    )
    (chapters / "ch_02_translation.txt").write_text(
        "第二章\n\n傅。\n\n吧。\n\n巴。", encoding="utf-8",
    )
    manifest = {
        "book_stem": "tiny_book",
        "title": "Tiny Book",
        "authors": ["Test Author"],
        "language": "en",
        "spine": [
            {"id": "item_001", "src_idref": "title", "original_idref": "title",
             "src_href": "OEBPS/xhtml/title.xhtml", "original_path": "OEBPS/xhtml/title.xhtml",
             "href": "chapters/item_001.html", "linear": "yes",
             "media_type": "application/xhtml+xml", "role": "title_page",
             "char_count": 9, "first_heading": "Title Page",
             "output_strategy": "source_only"},
            {"id": "item_002", "src_idref": "x", "original_idref": "x",
             "src_href": "OEBPS/xhtml/x.xhtml", "original_path": "OEBPS/xhtml/x.xhtml",
             "href": "chapters/item_002.html", "linear": "yes",
             "media_type": "application/xhtml+xml", "role": "body",
             "char_count": 12, "first_heading": "Chapter 1",
             "output_strategy": "translate", "translation_id": "ch_01"},
            {"id": "item_003", "src_idref": "y", "original_idref": "y",
             "src_href": "OEBPS/xhtml/y.xhtml", "original_path": "OEBPS/xhtml/y.xhtml",
             "href": "chapters/item_003.html", "linear": "yes",
             "media_type": "application/xhtml+xml", "role": "body",
             "char_count": 9, "first_heading": "Chapter 2",
             "output_strategy": "translate", "translation_id": "ch_02"},
        ],
        "chapters": [
            {"id": "ch_01", "spine_id": "item_002", "href": "chapters/item_002.html",
             "src_href": "OEBPS/xhtml/x.xhtml", "original_path": "OEBPS/xhtml/x.xhtml",
             "char_count": 12, "first_heading": "Chapter 1",
             "output_strategy": "translate"},
            {"id": "ch_02", "spine_id": "item_003", "href": "chapters/item_003.html",
             "src_href": "OEBPS/xhtml/y.xhtml", "original_path": "OEBPS/xhtml/y.xhtml",
             "char_count": 9, "first_heading": "Chapter 2",
             "output_strategy": "translate"},
        ],
    }
    (book_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return book_dir


def test_assemble_produces_valid_zip(tmp_path: Path):
    book_dir = _make_book_dir(tmp_path)
    out = tmp_path / "tiny_bilingual.epub"
    assemble.assemble(book_dir, out)
    assert out.is_file()
    assert out.stat().st_size > 0
    # EPUB is a zip file
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
    assert any(n == "mimetype" for n in names)
    assert "OEBPS/xhtml/title.xhtml" in names
    assert "OEBPS/xhtml/x.xhtml" in names
    assert "OEBPS/xhtml/y.xhtml" in names


def test_assemble_interleaves_source_and_translation(tmp_path: Path):
    book_dir = _make_book_dir(tmp_path)
    out = tmp_path / "tiny_bilingual.epub"
    assemble.assemble(book_dir, out)
    with zipfile.ZipFile(out) as z:
        ch1 = z.read("OEBPS/xhtml/x.xhtml").decode("utf-8")
    # Hello / World source must appear; 你好 / 世界 translation must appear
    assert "Hello." in ch1 and "你好" in ch1
    assert "World." in ch1 and "世界" in ch1
    # And source must come before its translation paragraph
    assert ch1.index("Hello.") < ch1.index("你好")


def test_assemble_warns_on_paragraph_count_mismatch(tmp_path: Path, capsys):
    book_dir = _make_book_dir(tmp_path)
    # Replace ch_02 translation with only 1 paragraph (source has 3)
    (book_dir / "chapters" / "ch_02_translation.txt").write_text("一段。", encoding="utf-8")
    out = tmp_path / "out.epub"
    assemble.assemble(book_dir, out)
    err = capsys.readouterr().err
    assert "paragraph count mismatch" in err
    assert "ch_02" in err
    # Output still produced
    assert out.is_file()
    # Missing translations stay source-only instead of receiving placeholder tgt text.
    with zipfile.ZipFile(out) as z:
        ch2 = z.read("OEBPS/xhtml/y.xhtml").decode("utf-8")
    assert "Bar." in ch2 and "Baz." in ch2
    assert "繁中：" not in ch2


def test_assemble_embeds_inline_images(tmp_path: Path):
    """Standalone <div><img/></div> blocks in source must produce <img> in output
    AND copy the corresponding image file into the EPUB at images/<filename>."""
    book_dir = _make_book_dir(tmp_path)
    # Add a standalone image block to ch_01's source HTML
    (book_dir / "chapters" / "item_002.html").write_text(
        '<html><body>'
        '<h1>Chapter 1</h1>'
        '<p>Hello.</p>'
        '<div class="figure"><img src="../images/diagram.png" alt="a diagram"/></div>'
        '<p>World.</p>'
        '</body></html>',
        encoding="utf-8",
    )
    # Translation has the same 2 text paragraphs (image not translated)
    (book_dir / "chapters" / "ch_01_translation.txt").write_text(
        "第一章\n\n你好。\n\n世界。", encoding="utf-8",
    )
    # Place the referenced image file in book_dir/images/
    (book_dir / "images").mkdir(exist_ok=True)
    # Minimal PNG bytes (1x1 transparent pixel)
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c6300010000000500010d0a2db40000000049454e44"
        "ae426082"
    )
    (book_dir / "images" / "diagram.png").write_bytes(png_bytes)

    out = tmp_path / "out.epub"
    assemble.assemble(book_dir, out)

    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        # image file embedded in EPUB
        assert any(n.endswith("images/diagram.png") for n in names)
        # chapter xhtml preserves the original relative image href.
        ch1 = z.read("OEBPS/xhtml/x.xhtml").decode("utf-8")
        assert 'src="../images/diagram.png"' in ch1
        # image block sits between the two text-block pairs
        assert ch1.index("Hello.") < ch1.index("diagram.png") < ch1.index("World.")
        # Source paragraphs still pair with translations correctly
        assert "你好" in ch1 and "世界" in ch1


def test_assemble_warns_on_missing_image_file(tmp_path: Path, capsys):
    """If chapter references an image not in book_dir/images/, warn but still build."""
    book_dir = _make_book_dir(tmp_path)
    (book_dir / "chapters" / "item_002.html").write_text(
        '<p>Hello.</p><div><img src="../images/missing.jpg" alt=""/></div><p>World.</p>',
        encoding="utf-8",
    )
    (book_dir / "chapters" / "ch_01_translation.txt").write_text(
        "你好。\n\n世界。", encoding="utf-8",
    )
    out = tmp_path / "out.epub"
    assemble.assemble(book_dir, out)
    err = capsys.readouterr().err
    assert "missing.jpg" in err
    # EPUB still produced
    assert out.is_file()


def test_assemble_fails_on_missing_translation_for_translate_item(tmp_path: Path):
    book_dir = _make_book_dir(tmp_path)
    (book_dir / "chapters" / "ch_02_translation.txt").unlink()
    out = tmp_path / "out.epub"
    try:
        assemble.assemble(book_dir, out)
    except ValueError as exc:
        assert "missing translation for translate item" in str(exc)
        assert "item_003" in str(exc)
    else:
        raise AssertionError("assemble should fail on missing translate item")
    assert not out.exists()


def test_assemble_source_only_items_in_output_spine(tmp_path: Path):
    book_dir = _make_book_dir(tmp_path)
    out = tmp_path / "tiny_bilingual.epub"
    assemble.assemble(book_dir, out)
    b = assemble.epub.read_epub(str(out), options={"ignore_ncx": True})
    spine_ids = [item[0] if isinstance(item, tuple) else item for item in b.spine]
    assert "title" in spine_ids
    assert "x" in spine_ids
    assert "y" in spine_ids


def test_assemble_embeds_all_images_not_just_referenced(tmp_path: Path):
    book_dir = _make_book_dir(tmp_path)
    images = book_dir / "images"
    images.mkdir(exist_ok=True)
    (images / "unused.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    out = tmp_path / "out.epub"
    assemble.assemble(book_dir, out)
    with zipfile.ZipFile(out) as z:
        assert "OEBPS/images/unused.jpg" in z.namelist()


def test_assemble_emits_source_only_at_original_relative_path(tmp_path: Path):
    book_dir = _make_book_dir(tmp_path)
    out = tmp_path / "out.epub"
    assemble.assemble(book_dir, out)
    with zipfile.ZipFile(out) as z:
        assert "OEBPS/xhtml/title.xhtml" in z.namelist()


def test_assemble_inserts_translation_after_each_english_paragraph(tmp_path: Path):
    book_dir = _make_book_dir(tmp_path)
    out = tmp_path / "out.epub"
    assemble.assemble(book_dir, out)
    with zipfile.ZipFile(out) as z:
        ch1 = z.read("OEBPS/xhtml/x.xhtml").decode("utf-8")
    assert ch1.index("Hello.") < ch1.index("你好。") < ch1.index("World.") < ch1.index("世界。")
    assert "tgt-zh" in ch1

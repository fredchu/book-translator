from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from bilingual_rewriter import insert_bilingual  # type: ignore  # noqa: E402


def test_insert_bilingual_interleaves_translated_paragraphs():
    html = "<html><body><h1>Chapter 1</h1><p>Hello.</p><p>World.</p></body></html>"
    entry = {
        "id": "item_001",
        "output_strategy": "translate",
        "original_idref": "chap1",
        "_translations_extra": {"nav_overrides": {"chap1": "第一章"}},
    }

    out, warnings = insert_bilingual(html, entry, ["你好。", "世界。"])

    assert warnings == []
    assert "Chapter 1" in out and "第一章" in out
    assert out.index("Hello.") < out.index("你好。") < out.index("World.") < out.index("世界。")
    assert 'class="src"' in out
    assert "tgt-zh" in out


def test_insert_bilingual_contents_links_receive_structural_translation():
    html = '<html><body><p><a href="chap.xhtml">Contents</a></p></body></html>'
    entry = {"id": "toc", "role": "contents", "output_strategy": "source_only"}

    out, warnings = insert_bilingual(html, entry, [])

    assert warnings == []
    assert "Contents ｜ 目錄" in out


def test_promoted_header_heading_does_not_consume_body_translation():
    # dispatch (extract_blocks) strips <header>, so the chapter-number heading
    # "1" is NOT translated; translations cover title + body paragraphs only.
    # assemble must not let the promoted "1" greedily consume a body translation.
    html = (
        "<html><body>"
        "<header><h1>1</h1></header>"
        "<h1>THE TITLE</h1>"
        "<p>First paragraph.</p><p>Second paragraph.</p>"
        "</body></html>"
    )
    entry = {"id": "item_008", "output_strategy": "translate", "original_idref": "chap1"}

    out, warnings = insert_bilingual(html, entry, ["標題", "第一段。", "第二段。"])

    assert warnings == []
    # body must align in order, not shifted by the bare "1"
    assert out.index("THE TITLE") < out.index("標題")
    assert (
        out.index("First paragraph.")
        < out.index("第一段。")
        < out.index("Second paragraph.")
        < out.index("第二段。")
    )


def test_promoted_heading_does_not_duplicate_nav_title():
    # With nav_overrides present (ToC labels), a promoted chapter-number heading
    # must NOT pull the per-chapter title as its sibling — that title already
    # renders on the in-body title heading and would otherwise appear twice.
    html = (
        "<html><body>"
        "<header><h1>1</h1></header>"
        "<h1>THE TITLE</h1>"
        "<p>Body.</p>"
        "</body></html>"
    )
    entry = {
        "id": "item_008",
        "output_strategy": "translate",
        "original_idref": "chap1",
        "_translations_extra": {"nav_overrides": {"chap1": "標題譯文"}},
    }

    out, warnings = insert_bilingual(html, entry, ["標題譯文", "正文。"])

    assert warnings == []
    assert out.count("標題譯文") == 1
    assert out.index("THE TITLE") < out.index("標題譯文")


def test_insert_bilingual_warns_on_paragraph_mismatch():
    html = "<html><body><p>One.</p><p>Two.</p></body></html>"
    entry = {"id": "item_001", "translation_id": "ch_01", "output_strategy": "translate"}

    out, warnings = insert_bilingual(html, entry, ["一。"])

    assert "一。" in out
    assert warnings == ["ch_01: paragraph count mismatch (src_text=2 tgt=1); pairing available paragraphs"]

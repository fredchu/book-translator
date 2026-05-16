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


def test_insert_bilingual_warns_on_paragraph_mismatch():
    html = "<html><body><p>One.</p><p>Two.</p></body></html>"
    entry = {"id": "item_001", "translation_id": "ch_01", "output_strategy": "translate"}

    out, warnings = insert_bilingual(html, entry, ["一。"])

    assert "一。" in out
    assert warnings == ["ch_01: paragraph count mismatch (src_text=2 tgt=1); pairing available paragraphs"]

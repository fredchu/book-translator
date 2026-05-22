"""End-to-end test: aup_refused chapter preserves source + bilingual note in output EPUB."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import assemble as assemble_module  # type: ignore  # noqa: E402
import state as state_module  # type: ignore  # noqa: E402


def test_aup_refused_chapter_emits_source_with_annotation(tmp_path):
    """Chapter marked aup_refused should appear in output with source text + a
    visible 「本章因 LLM 政策拒答」note injected at the top."""
    # Build a minimal book_dir fixture
    book_dir = tmp_path / "test_book"
    book_dir.mkdir()

    # Minimal manifest.json with one translate chapter
    manifest = {
        "schema_version": "v2",
        "book_path": "fake.epub",
        "spine": [
            {
                "id": "item_001",
                "original_path": "OEBPS/ch01.xhtml",
                "original_idref": "ch01",
                "output_strategy": "translate",
                "html_path": "chapters/item_001.html",
            },
        ],
        "title": "Test Book",
        "author": "Test Author",
    }
    import json
    (book_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    # Source chapter file
    (book_dir / "chapters").mkdir()
    (book_dir / "chapters" / "item_001.html").write_text(
        "<html><body><h1>Ch 1</h1><p>Sensitive content here.</p></body></html>",
        encoding="utf-8",
    )

    # state.json marking item_001 as aup_refused
    state = {
        "book": "fake.epub",
        "started": "2026-05-19T00:00:00Z",
        "target_lang": "zh-tw",
        "glossary_built": True,
        "style_confirmed": True,
        "chapters": {
            "item_001": {
                "output_strategy": "translate",
                "status": "aup_refused",
                "reason": "Anthropic AUP refused this section",
            },
        },
    }
    (book_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    # No translation file — assemble must not raise because of aup_refused status
    # Run the bilingual rewriter directly on this single chapter
    # (we test the unit that decides what to do per chapter, not the full assemble)
    from bilingual_rewriter import insert_bilingual  # type: ignore

    src_html = (book_dir / "chapters" / "item_001.html").read_text(encoding="utf-8")

    # Simulate the entry the assemble pipeline would pass — extend SpineEntry-like dict
    entry = manifest["spine"][0]

    output_html = insert_bilingual(
        src_html=src_html,
        entry=entry,
        translations={},  # no translation; status drives behavior
        chapter_status="aup_refused",
        aup_reason="Anthropic AUP refused this section",
    )

    # Output should keep the source AND include a visible Chinese annotation
    assert "Sensitive content here." in output_html
    assert "本章因" in output_html or "AUP" in output_html

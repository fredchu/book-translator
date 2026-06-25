"""Offline header-title nav fix (2026-06-25).

Root cause: chapter titles that live in a <header> (chapter-number heading +
role="doc-subtitle") are dropped by strip_non_content, so build_nav_overrides
never produces a Chinese nav label and the EPUB ToC renders English-only.
translate_header_titles extracts those titles and batch-translates them through
the provider. The translation_quality audit also gained a guard so inline-bilingual
ToC links ("English ｜ 中文") on source_only pages are not flagged as untranslated.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import offline_postprocess as op  # noqa: E402
import translation_quality_audit as tqa  # noqa: E402


_HEADER_CHAPTER = """<?xml version='1.0' encoding='utf-8'?>
<html xmlns="http://www.w3.org/1999/xhtml"><body><div>
  <header>
    <h1 class="cn-chap-pg">Chapter 1</h1>
    <p class="ct" role="doc-subtitle">The Meaning of Meaning</p>
  </header>
  <p class="pf">At the age of fifty-one, the novelist wanted to quit.</p>
</div></body></html>
"""

_BARE_HEADING = """<html><body><h1>Acknowledgments</h1>
  <p>Thanks to everyone.</p></body></html>
"""


def _make_book(tmp_path: Path, html: str, idref: str, item_id: str) -> Path:
    book_dir = tmp_path / "book"
    (book_dir / "chapters").mkdir(parents=True)
    (book_dir / "chapters" / f"{item_id}.html").write_text(html, encoding="utf-8")
    manifest = {"spine": [{"id": item_id, "original_idref": idref, "output_strategy": "translate"}]}
    (book_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return book_dir


def test_extract_header_title_combines_number_and_subtitle(tmp_path: Path) -> None:
    p = tmp_path / "ch.html"
    p.write_text(_HEADER_CHAPTER, encoding="utf-8")
    assert op._extract_header_title(p) == "Chapter 1 : The Meaning of Meaning"


def test_extract_header_title_falls_back_to_first_heading(tmp_path: Path) -> None:
    p = tmp_path / "ack.html"
    p.write_text(_BARE_HEADING, encoding="utf-8")
    assert op._extract_header_title(p) == "Acknowledgments"


class _MarkerEchoProvider:
    """Mock provider that echoes the [[Tn]] markers with a fixed translation."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def translate(self, prompt, *, request_id=None, system=None, log_dir=None):
        self.calls.append(prompt)
        from providers.base import ProviderResult

        return ProviderResult(
            raw_text="[[T1]] 第1章：意義的意義",
            model="mock",
            latency_ms=1,
            retries=0,
            metadata={},
        )


def test_translate_header_titles_writes_nav_override(tmp_path: Path) -> None:
    book_dir = _make_book(tmp_path, _HEADER_CHAPTER, "x007_c001", "item_007")
    manifest = json.loads((book_dir / "manifest.json").read_text(encoding="utf-8"))
    provider = _MarkerEchoProvider()
    added = op.translate_header_titles(book_dir, manifest, provider)
    assert added == 1
    assert len(provider.calls) == 1
    extra = json.loads((book_dir / "translations_extra.json").read_text(encoding="utf-8"))
    assert extra["nav_overrides"]["x007_c001"] == "第1章：意義的意義"


def test_translate_header_titles_skips_existing_override(tmp_path: Path) -> None:
    book_dir = _make_book(tmp_path, _HEADER_CHAPTER, "x007_c001", "item_007")
    (book_dir / "translations_extra.json").write_text(
        json.dumps({"nav_overrides": {"x007_c001": "既有"}}), encoding="utf-8"
    )
    manifest = json.loads((book_dir / "manifest.json").read_text(encoding="utf-8"))
    provider = _MarkerEchoProvider()
    added = op.translate_header_titles(book_dir, manifest, provider)
    assert added == 0
    assert provider.calls == []  # nothing pending → provider never called


def test_translate_header_titles_no_provider_is_noop(tmp_path: Path) -> None:
    book_dir = _make_book(tmp_path, _HEADER_CHAPTER, "x007_c001", "item_007")
    manifest = json.loads((book_dir / "manifest.json").read_text(encoding="utf-8"))
    assert op.translate_header_titles(book_dir, manifest, None) == 0


def test_audit_contains_han_guard_exempts_inline_bilingual() -> None:
    # A source paragraph that already carries its translation inline ("EN ｜ 中文")
    # must not be flagged as an untranslated source-only paragraph.
    assert tqa._contains_han("Dedication ｜ 獻辭") is True
    assert tqa._contains_han("Dedication") is False

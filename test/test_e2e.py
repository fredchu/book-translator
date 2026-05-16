"""End-to-end smoke test: real EPUB → extract → (fake translate) → assemble → valid bilingual EPUB.

This exercises the full deterministic pipeline without burning LLM tokens. Translation
quality is gated separately by Phase 3 cross-modal eval (run on real Opus 4.7
translations during actual book runs).
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT / "scripts"))
import assemble  # type: ignore  # noqa: E402
import dispatch  # type: ignore  # noqa: E402
import extract_epub  # type: ignore  # noqa: E402
import state  # type: ignore  # noqa: E402


FIXTURE = Path.home() / "ghkb/interested/bilingual_book_maker/test_books/animal_farm.epub"


def _fake_translate(chapter_html: str) -> str:
    """Stand-in for the real subagent translation.

    Produces the same paragraph count as the source (so assemble pairs cleanly).
    Each paragraph is prefixed with [ZH] so we can verify provenance in the output.
    """
    paragraphs = dispatch.html_to_paragraphs(chapter_html)
    # Skip the first paragraph if it's a heading that will be rendered as <h1>
    # — assemble dedups against manifest first_heading, but our fake heading is
    # "(untitled)" for Animal Farm, so the dedup won't fire and we keep all paragraphs.
    return "\n\n".join(f"[ZH] {p}" for p in paragraphs)


@pytest.mark.skipif(not FIXTURE.is_file(), reason=f"fixture missing: {FIXTURE}")
def test_e2e_animal_farm_full_pipeline(tmp_path: Path):
    # 1. Extract
    book_dir = extract_epub.extract(FIXTURE, tmp_path)
    manifest = json.loads((book_dir / "manifest.json").read_text("utf-8"))
    assert len(manifest["chapters"]) >= 10  # Animal Farm has 10 body chapters

    # 2. Init state, run "translation" for every chapter (deterministic fake)
    chapter_ids = [c["id"] for c in manifest["chapters"]]
    s = state.init_state(FIXTURE, chapter_ids, target_lang="zh-tw")
    state_path = book_dir / "state.json"
    state.save(state_path, s)

    for entry in manifest["chapters"]:
        chap_id = entry["id"]
        chap_html = (book_dir / entry["href"]).read_text("utf-8")
        translation = _fake_translate(chap_html)
        # Validate the fake translation (paragraph count should match)
        warnings = dispatch.validate_translation(translation, chap_html)
        # Our fake translation preserves paragraph count, so no warnings expected
        assert warnings == [], f"fake translate produced warnings for {chap_id}: {warnings}"
        # Write per-chapter translation file
        (book_dir / "chapters" / f"{chap_id}_translation.txt").write_text(
            translation, encoding="utf-8"
        )
        # Update state
        state.mark_done(s, chap_id, translation)

    # State persists and reports all chapters done
    state.save(state_path, s)
    reloaded = state.load(state_path)
    assert reloaded is not None
    assert state.chapters_by_status(reloaded, "done") == chapter_ids
    assert state.chapters_by_status(reloaded, "pending") == []

    # 3. Assemble
    out_epub = tmp_path / "animal_farm_bilingual.epub"
    assemble.assemble(book_dir, out_epub, strict_nav=False)

    # 4. Validate the produced EPUB
    assert out_epub.is_file()
    assert out_epub.stat().st_size > 0
    with zipfile.ZipFile(out_epub) as z:
        names = z.namelist()
        assert "mimetype" in names
        # One xhtml per chapter
        ch_xhtmls = [n for n in names if n.endswith(".xhtml") and "/ch_" in n]
        assert len(ch_xhtmls) == len(manifest["chapters"])
        # Sample one chapter — must contain interleaved source + fake translation
        sample = z.read(ch_xhtmls[1]).decode("utf-8")  # skip cover/colophon
        assert "[ZH]" in sample, "fake translation marker missing"
        assert '<p class="src">' in sample
        assert '<p class="tgt">' in sample


@pytest.mark.skipif(not FIXTURE.is_file(), reason=f"fixture missing: {FIXTURE}")
def test_e2e_resume_skips_completed(tmp_path: Path):
    """Resume scenario: state.json says ch_01 is done → next run skips it."""
    book_dir = extract_epub.extract(FIXTURE, tmp_path)
    manifest = json.loads((book_dir / "manifest.json").read_text("utf-8"))
    chapter_ids = [c["id"] for c in manifest["chapters"]]

    s = state.init_state(FIXTURE, chapter_ids, target_lang="zh-tw")
    state.mark_done(s, chapter_ids[0], "[ZH] fake translation for ch_01")
    state.save(book_dir / "state.json", s)

    pending = state.chapters_by_status(s, "pending")
    done = state.chapters_by_status(s, "done")
    assert chapter_ids[0] in done
    assert chapter_ids[0] not in pending
    assert len(pending) == len(chapter_ids) - 1

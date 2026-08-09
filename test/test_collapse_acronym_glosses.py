"""Spec §5.2 — first mention keeps 中譯（ACRONYM）, later mentions collapse to the acronym.

The model cannot do this itself: it sees one chunk at a time and treats every
chunk as the term's first mention (measured 2026-08-09: 人工智慧 x10 in a single
chapter, in two independent runs).
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import offline_postprocess as pp  # noqa: E402


def _book(tmp_path: Path, *chapters: str) -> Path:
    (tmp_path / "chapters").mkdir(parents=True, exist_ok=True)
    for i, text in enumerate(chapters, start=1):
        (tmp_path / "chapters" / f"item_{i:03d}_translation.txt").write_text(
            text, encoding="utf-8")
    return tmp_path


def _read(book: Path, n: int = 1) -> str:
    return (book / "chapters" / f"item_{n:03d}_translation.txt").read_text(encoding="utf-8")


def test_first_gloss_kept_later_mentions_collapsed(tmp_path: Path) -> None:
    book = _book(tmp_path, "人工智慧（AI）正在改變世界。\n\n人工智慧也有風險。\n\n談到人工智慧時要小心。")
    result = pp.collapse_acronym_glosses(book)
    out = _read(book)
    assert out.count("人工智慧（AI）") == 1
    assert out.count("人工智慧") == 1, out
    assert "AI也有風險" in out and "談到AI時" in out
    assert result == [("人工智慧", "AI", 2)]


def test_collapse_spans_chapters(tmp_path: Path) -> None:
    book = _book(tmp_path, "大型語言模型（LLM）很強。", "大型語言模型會出錯。")
    pp.collapse_acronym_glosses(book)
    assert _read(book, 1).count("大型語言模型（LLM）") == 1
    assert _read(book, 2).strip() == "LLM會出錯。"


def test_chinese_rendering_is_learned_not_hardcoded(tmp_path: Path) -> None:
    """A book that glosses AI as 人工智能 collapses that, not a table entry."""
    book = _book(tmp_path, "人工智能（AI）如何運作。\n\n人工智能的極限。")
    pp.collapse_acronym_glosses(book)
    out = _read(book)
    assert out.count("人工智能") == 1
    assert "AI的極限" in out


def test_untouched_when_no_gloss_present(tmp_path: Path) -> None:
    """Without a 中譯（ACRONYM）anchor there is nothing to learn, so leave it alone."""
    original = "人工智慧改變了世界。\n\n人工智慧也有風險。"
    book = _book(tmp_path, original)
    assert pp.collapse_acronym_glosses(book) == []
    assert _read(book) == original


def test_only_target_terms_change(tmp_path: Path) -> None:
    original = "人工智慧（AI）與機器學習不同。\n\n機器學習是人工智慧的一支。"
    book = _book(tmp_path, original)
    pp.collapse_acronym_glosses(book)
    out = _read(book)
    assert out.count("機器學習") == 2, "non-target terms must be untouched"
    assert "機器學習是AI的一支" in out


def test_empty_book_dir_is_safe(tmp_path: Path) -> None:
    (tmp_path / "chapters").mkdir()
    assert pp.collapse_acronym_glosses(tmp_path) == []

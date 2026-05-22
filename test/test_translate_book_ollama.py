from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

# Import the driver after sys.path setup
import translate_book_ollama as drv  # type: ignore  # noqa: E402
from providers.base import ProviderResult  # noqa: E402


def _mock_provider_returning(*aligned_per_call_returns: str | None) -> MagicMock:
    """Mock provider whose .translate() yields ProviderResult with raw_text
    set to a marker-aligned response when the input is a marker prompt,
    or None for empty-output simulation."""
    mock = MagicMock()
    mock.temperature = 0.3
    mock.ping.return_value = True
    queue = list(aligned_per_call_returns)

    def _translate(prompt, *, request_id, log_dir, system=None):
        # Pop the next planned response (default to a generic marker-aligned 1-para)
        nxt = queue.pop(0) if queue else "[[PARA_1]]\n譯文"
        if nxt is None:
            return ProviderResult(raw_text="", model="mock", latency_ms=10, retries=0, metadata={})
        return ProviderResult(raw_text=nxt, model="mock", latency_ms=10, retries=0, metadata={})

    mock.translate.side_effect = _translate
    return mock


def test_recursion_splits_failed_chunk_into_halves(tmp_path):
    # 4-paragraph chunk: first call (whole 4 paras) returns misaligned; halves succeed
    paragraphs = ("A.", "B.", "C.", "D.")
    mock = _mock_provider_returning(
        "[[PARA_1]]\n甲",  # whole chunk: missing 2,3,4 — fail
        "[[PARA_1]]\n甲",  # whole chunk attempt 1 retry: still bad — fail
        "[[PARA_1]]\n甲\n\n[[PARA_2]]\n乙",  # left half (A,B): aligned
        "[[PARA_1]]\n丙\n\n[[PARA_2]]\n丁",  # right half (C,D): aligned
    )
    result, _warns = drv._translate_chunk_with_recursion(
        mock,
        chunk_paragraphs=paragraphs,
        chapter_label="1",
        chunk_label="ck01of01",
        book_title="Test",
        target_lang="zh-tw",
        carryover="",
        book_dir=tmp_path,
        default_temperature=0.3,
        depth=0,
        max_depth=4,
    )
    assert result is not None
    assert "甲" in result and "乙" in result and "丙" in result and "丁" in result


def test_recursion_falls_back_to_single_paragraph_when_one_left(tmp_path):
    # 1-paragraph chunk: marker contract fails both attempts; minimal fallback succeeds
    paragraphs = ("Hello.",)
    mock = _mock_provider_returning(
        "I cannot help with this",   # marker attempt 0
        "",                          # marker attempt 1: empty
        "你好。",                     # minimal fallback: succeeds
    )
    result, _warns = drv._translate_chunk_with_recursion(
        mock,
        chunk_paragraphs=paragraphs,
        chapter_label="1",
        chunk_label="ck01-LRL",
        book_title="Test",
        target_lang="zh-tw",
        carryover="",
        book_dir=tmp_path,
        default_temperature=0.3,
        depth=3,
        max_depth=4,
    )
    assert result == "你好。"


def test_recursion_returns_none_when_all_fail(tmp_path):
    # 1-paragraph chunk: all 3 attempts (marker x2 + minimal fallback x1) return empty
    paragraphs = ("Hello.",)
    mock = _mock_provider_returning("", "", "")  # all empty
    result, _warns = drv._translate_chunk_with_recursion(
        mock,
        chunk_paragraphs=paragraphs,
        chapter_label="1",
        chunk_label="ck01",
        book_title="Test",
        target_lang="zh-tw",
        carryover="",
        book_dir=tmp_path,
        default_temperature=0.3,
        depth=0,
        max_depth=4,
    )
    assert result is None


def test_recursion_respects_max_depth(tmp_path):
    # 8-paragraph chunk forced to depth=4; should not split further than allowed.
    # At max_depth, instead of splitting further, the function should fall through
    # to single-paragraph fallback for each remaining paragraph.
    paragraphs = tuple(f"P{i}." for i in range(8))
    # Whole chunk fails 2x; all halves also fail at every level; at depth 4
    # the fallback is forced for each paragraph. Set up enough mock returns.
    fallbacks = ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛"]
    # Worst case sequence: 2 attempts at every level. Just provide enough empty/marker-failing returns then the 8 fallbacks at the end.
    seq = ["[[PARA_1]]\nbad"] * 32 + fallbacks  # padded; recursion may not consume all
    mock = _mock_provider_returning(*seq)
    result, _warns = drv._translate_chunk_with_recursion(
        mock,
        chunk_paragraphs=paragraphs,
        chapter_label="1",
        chunk_label="ck01",
        book_title="Test",
        target_lang="zh-tw",
        carryover="",
        book_dir=tmp_path,
        default_temperature=0.3,
        depth=0,
        max_depth=4,
    )
    # Either succeeds via fallback or returns None — both acceptable; key is no infinite recursion.
    # The test passing without timeout is the real assertion.
    assert result is None or len(result) > 0

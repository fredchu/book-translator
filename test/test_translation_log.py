"""Tests for translation_log — per-chapter prompt+response persistence."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import translation_log as tlog  # type: ignore  # noqa: E402


def test_write_log_entry_creates_translation_log_dir(tmp_path):
    tlog.write_log_entry(
        book_dir=tmp_path,
        chapter_id="item_002",
        prompt="prompt text",
        raw_response="[[PARA_1]]\n甲",
        parsed_translation="甲",
        validation_warnings=[],
        model="sonnet",
    )
    log_dir = tmp_path / "translation_log"
    assert log_dir.is_dir()
    entry_path = log_dir / "item_002.json"
    assert entry_path.exists()


def test_write_log_entry_payload_round_trips(tmp_path):
    tlog.write_log_entry(
        book_dir=tmp_path,
        chapter_id="item_005",
        prompt="P",
        raw_response="R",
        parsed_translation="T",
        validation_warnings=["w1"],
        model="opus",
    )
    entry = tlog.read_log_entry(book_dir=tmp_path, chapter_id="item_005")
    assert entry["chapter_id"] == "item_005"
    assert entry["model"] == "opus"
    assert entry["prompt"] == "P"
    assert entry["raw_response"] == "R"
    assert entry["parsed_translation"] == "T"
    assert entry["validation_warnings"] == ["w1"]
    assert "timestamp" in entry


def test_write_log_entry_records_marker_count_when_aligned(tmp_path):
    raw = "[[PARA_1]]\n甲\n\n[[PARA_2]]\n乙"
    tlog.write_log_entry(
        book_dir=tmp_path, chapter_id="item_003", prompt="P", raw_response=raw,
        parsed_translation="甲\n\n乙", validation_warnings=[],
        model="sonnet", source_paragraph_count=2,
    )
    entry = tlog.read_log_entry(book_dir=tmp_path, chapter_id="item_003")
    assert entry["source_paragraph_count"] == 2
    assert entry.get("response_marker_count") == 2


def test_read_log_entry_returns_none_when_missing(tmp_path):
    assert tlog.read_log_entry(book_dir=tmp_path, chapter_id="nope") is None

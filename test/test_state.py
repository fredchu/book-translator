"""Unit tests for state.py — resume state machine."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import state  # type: ignore  # noqa: E402


def test_init_state_marks_all_pending(tmp_path: Path):
    s = state.init_state(tmp_path / "x.epub", ["ch_01", "ch_02", "ch_03"], "zh-tw")
    assert s["book"] == "x.epub"
    assert s["target_lang"] == "zh-tw"
    assert s["glossary_built"] is False
    assert s["style_confirmed"] is False
    assert all(c["status"] == "pending" for c in s["chapters"].values())


def test_init_state_declares_output_strategy_for_spine(tmp_path: Path):
    s = state.init_state(
        tmp_path / "x.epub",
        [
            {"id": "item_001", "output_strategy": "source_only"},
            {"id": "item_002", "output_strategy": "translate"},
        ],
        "zh-tw",
    )
    assert s["chapters"]["item_001"] == {
        "output_strategy": "source_only",
        "status": "pending",
    }
    assert s["chapters"]["item_002"] == {
        "output_strategy": "translate",
        "status": "pending",
    }


def test_roundtrip_save_load(tmp_path: Path):
    s = state.init_state(tmp_path / "x.epub", ["ch_01"], "zh-tw")
    p = tmp_path / "state.json"
    state.save(p, s)
    loaded = state.load(p)
    assert loaded == s


def test_load_missing_returns_none(tmp_path: Path):
    assert state.load(tmp_path / "does_not_exist.json") is None


def test_state_rejects_unknown_status():
    s = {"chapters": {"item_001": {"output_strategy": "translate", "status": "skipped"}}}
    try:
        state.validate_state(s)
    except ValueError as exc:
        assert "unknown status" in str(exc)
        assert "skipped" in str(exc)
    else:
        raise AssertionError("validate_state should reject skipped")


def test_mark_done_stores_hash_and_carryover():
    s = {"chapters": {"ch_01": {"status": "pending"}}}
    text = "abc " * 100  # 400 chars
    state.mark_done(s, "ch_01", text)
    entry = s["chapters"]["ch_01"]
    assert entry["status"] == "done"
    assert len(entry["translation_hash"]) == 12
    assert entry["carryover"] == text[-200:]


def test_mark_done_short_text_keeps_all():
    s = {"chapters": {"ch_01": {"status": "pending"}}}
    state.mark_done(s, "ch_01", "tiny")
    assert s["chapters"]["ch_01"]["carryover"] == "tiny"


def test_mark_failed_increments_retry():
    s = {"chapters": {"ch_02": {"status": "pending"}}}
    state.mark_failed(s, "ch_02", "timeout")
    assert s["chapters"]["ch_02"] == {"status": "failed", "retry_count": 1, "error": "timeout"}
    state.mark_failed(s, "ch_02", "timeout again")
    assert s["chapters"]["ch_02"]["retry_count"] == 2


def test_chapters_by_status():
    s = {"chapters": {
        "ch_01": {"status": "done"},
        "ch_02": {"status": "failed"},
        "ch_03": {"status": "pending"},
        "ch_04": {"status": "pending"},
    }}
    assert state.chapters_by_status(s, "pending") == ["ch_03", "ch_04"]
    assert state.chapters_by_status(s, "done") == ["ch_01"]
    assert state.chapters_by_status(s, "failed") == ["ch_02"]


def test_carryover_for_first_chapter_is_empty():
    s = {"chapters": {"ch_01": {"status": "pending"}}}
    assert state.carryover_for(s, "ch_01") == ""


def test_carryover_for_uses_previous_done_chapter():
    s = {"chapters": {
        "ch_01": {"status": "done", "carryover": "last bit of ch1"},
        "ch_02": {"status": "pending"},
    }}
    assert state.carryover_for(s, "ch_02") == "last bit of ch1"


def test_carryover_for_skips_unfinished_previous():
    s = {"chapters": {
        "ch_01": {"status": "pending"},
        "ch_02": {"status": "pending"},
    }}
    assert state.carryover_for(s, "ch_02") == ""

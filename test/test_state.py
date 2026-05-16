"""Unit tests for state.py — resume state machine."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

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


def test_chapter_entry_round_trips_known_fields():
    data = {
        "output_strategy": "translate",
        "status": "failed",
        "translation_hash": "abc123def456",
        "carryover": "previous text",
        "retry_count": 2,
        "error": "timeout",
        "reason": "manual drop",
    }

    entry = state.ChapterEntry.from_dict(data)

    assert entry.to_dict() == data


@pytest.mark.parametrize("output_strategy", sorted(state.VALID_OUTPUT_STRATEGIES))
@pytest.mark.parametrize(
    ("mutation", "args"),
    [
        (state.mark_done, ("translated text",)),
        (state.mark_failed, ("timeout",)),
        (state.mark_source_ready, ()),
    ],
)
def test_mark_mutations_preserve_output_strategy(output_strategy, mutation, args):
    s = {
        "chapters": {
            "ch_01": {
                "output_strategy": output_strategy,
                "status": "pending",
                "reason": "explicitly dropped" if output_strategy == state.DROP_EXPLICIT else None,
            }
        }
    }

    mutation(s, "ch_01", *args)

    assert s["chapters"]["ch_01"]["output_strategy"] == output_strategy


def test_mark_dropped_replaces_output_strategy():
    s = {"chapters": {"ch_01": {"output_strategy": "translate", "status": "pending"}}}

    state.mark_dropped(s, "ch_01", "not part of translated body")

    assert s["chapters"]["ch_01"] == {
        "output_strategy": "drop_explicit",
        "status": "dropped",
        "reason": "not part of translated body",
    }


def test_legacy_entries_without_output_strategy_mutate_without_crashing():
    missing_strategy_cases = [
        (state.mark_done, ("translated text",), "translate"),
        (state.mark_source_ready, (), "source_only"),
        (state.mark_failed, ("timeout",), None),
    ]

    for mutation, args, expected_strategy in missing_strategy_cases:
        s = {"chapters": {"ch_01": {"status": "pending"}}}

        mutation(s, "ch_01", *args)

        if expected_strategy is None:
            assert "output_strategy" not in s["chapters"]["ch_01"]
        else:
            assert s["chapters"]["ch_01"]["output_strategy"] == expected_strategy


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

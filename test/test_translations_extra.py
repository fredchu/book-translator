"""Tests for optional per-book assemble translation overrides."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import assemble  # type: ignore  # noqa: E402


def test_load_translations_extra_returns_empty_dict_when_missing(tmp_path: Path):
    assert assemble._load_translations_extra(tmp_path) == {}


def test_load_translations_extra_returns_parsed_dict_when_file_exists(tmp_path: Path):
    payload = {
        "by_exact_text": {"Contents": "自訂目錄"},
        "nav_overrides": {"chapter_1": "自訂章節"},
    }
    (tmp_path / "translations_extra.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

    assert assemble._load_translations_extra(tmp_path) == payload


def test_fallback_translation_extra_exact_text_wins_over_structural_label():
    entry = {"_translations_extra": {"by_exact_text": {"Contents": "自訂目錄"}}}

    assert assemble._fallback_translation("Contents", entry) == "自訂目錄"


def test_nav_label_extra_exact_text_wins_over_structural_label():
    entry = {
        "id": "contents",
        "first_heading": "Contents",
        "_translations_extra": {"by_exact_text": {"Contents": "自訂目錄"}},
    }

    assert assemble._nav_label(entry) == "Contents / 自訂目錄"

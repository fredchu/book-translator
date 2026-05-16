"""Tests for optional per-book assemble translation overrides."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import assemble  # type: ignore  # noqa: E402
import translations_extra  # type: ignore  # noqa: E402


def test_load_translations_extra_returns_empty_dict_when_missing(tmp_path: Path):
    assert assemble._load_translations_extra(tmp_path) == {}
    assert translations_extra.load(tmp_path) == {}


def test_load_translations_extra_returns_parsed_dict_when_file_exists(tmp_path: Path):
    payload = {
        "by_exact_text": {"Contents": "自訂目錄"},
        "nav_overrides": {"chapter_1": "自訂章節"},
    }
    (tmp_path / "translations_extra.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

    assert assemble._load_translations_extra(tmp_path) == payload
    assert translations_extra.load(tmp_path) == payload


def test_load_translations_extra_raises_for_malformed_json(tmp_path: Path):
    (tmp_path / "translations_extra.json").write_text("{", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        translations_extra.load(tmp_path)


def test_load_translations_extra_rejects_non_object(tmp_path: Path):
    (tmp_path / "translations_extra.json").write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="translations_extra must be a JSON object"):
        translations_extra.load(tmp_path)


def test_save_translations_extra_round_trip(tmp_path: Path):
    payload = {
        "by_exact_text": {"Contents": "自訂目錄"},
        "nav_overrides": {"Ac01": "第一章"},
    }

    translations_extra.save(tmp_path, payload)

    assert translations_extra.load(tmp_path) == payload


def test_write_nav_overrides_populates_and_preserves_existing_keys(tmp_path: Path):
    existing = {
        "by_exact_text": {"Contents": "自訂目錄"},
        "nav_overrides": {"Ac01": "手動第一章"},
        "other_key": {"keep": "me"},
    }
    translations_extra.save(tmp_path, existing)
    manifest = {
        "spine": [
            {"original_idref": "Ac01", "first_heading": "CHAPTER 1 The Past"},
            {"original_idref": "Ac02", "first_heading": "CHAPTER 2 The Future"},
            {"original_idref": "Ac03", "first_heading": "CHAPTER 3 Empty"},
        ]
    }
    glossary = {
        "chapter_titles_zh": {
            "CHAPTER 1 The Past": "第一章　過去",
            "CHAPTER 2 The Future": "第二章　未來",
            "CHAPTER 3 Empty": "",
        }
    }

    nav = translations_extra.write_nav_overrides(glossary, manifest, tmp_path)
    saved = translations_extra.load(tmp_path)

    assert nav == {"Ac01": "手動第一章", "Ac02": "第二章　未來"}
    assert saved["nav_overrides"] == nav
    assert saved["by_exact_text"] == {"Contents": "自訂目錄"}
    assert saved["other_key"] == {"keep": "me"}


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

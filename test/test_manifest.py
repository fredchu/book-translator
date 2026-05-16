"""Tests for manifest.py normalization helpers."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import manifest  # type: ignore  # noqa: E402


def test_spine_entry_construction():
    entry = manifest.SpineEntry(
        id="item_001",
        src_idref="chap01",
        src_href="OEBPS/ch01.xhtml",
        original_idref="chap01",
        original_path="OEBPS/ch01.xhtml",
        href="chapters/item_001.html",
        linear="yes",
        media_type="application/xhtml+xml",
        role="body",
        char_count=123,
        first_heading="Chapter 1",
        output_strategy="translate",
        translation_id="ch_01",
    )

    assert entry.id == "item_001"
    assert entry.translation_id == "ch_01"
    assert entry.as_dict()["parent_id"] is None
    assert "reason" not in entry.as_dict()


def test_normalize_entries_with_spine_key_preserves_values():
    entries = manifest.normalize_entries(
        {
            "spine": [
                {
                    "id": "item_001",
                    "src_idref": "chap01",
                    "src_href": "OEBPS/ch01.xhtml",
                    "original_idref": "chap01",
                    "original_path": "OEBPS/ch01.xhtml",
                    "href": "chapters/item_001.html",
                    "linear": "yes",
                    "media_type": "application/xhtml+xml",
                    "role": "body",
                    "char_count": 321,
                    "first_heading": "Chapter 1",
                    "output_strategy": "translate",
                    "translation_id": "ch_01",
                    "parent_id": None,
                }
            ]
        }
    )

    assert entries == [
        manifest.SpineEntry(
            id="item_001",
            src_idref="chap01",
            src_href="OEBPS/ch01.xhtml",
            original_idref="chap01",
            original_path="OEBPS/ch01.xhtml",
            href="chapters/item_001.html",
            linear="yes",
            media_type="application/xhtml+xml",
            role="body",
            char_count=321,
            first_heading="Chapter 1",
            output_strategy="translate",
            translation_id="ch_01",
            parent_id=None,
        )
    ]


def test_normalize_entries_with_legacy_chapters_backfills_defaults():
    entries = manifest.normalize_entries(
        {
            "chapters": [
                {
                    "id": "ch_01",
                    "spine_id": "item_002",
                    "href": "chapters/item_002.html",
                    "src_href": "OEBPS/ch01.xhtml",
                    "char_count": "99",
                    "first_heading": "Chapter 1",
                }
            ]
        }
    )

    entry = entries[0]
    assert entry.id == "item_002"
    assert entry.src_idref == "item_002"
    assert entry.original_idref == "item_002"
    assert entry.original_path == "OEBPS/ch01.xhtml"
    assert entry.linear == "yes"
    assert entry.media_type == manifest.XHTML_MEDIA_TYPE
    assert entry.role == "body"
    assert entry.char_count == 99
    assert entry.output_strategy == "translate"
    assert entry.translation_id == "ch_01"


def test_normalize_entries_with_none_or_empty_manifest():
    assert manifest.normalize_entries(None) == []
    assert manifest.normalize_entries({}) == []
    assert manifest.normalize_entries({"chapters": []}) == []


def test_chapters_from_spine_filters_translate_entries():
    entries = [
        manifest.SpineEntry(
            id="item_001",
            src_idref="title",
            src_href="OEBPS/title.xhtml",
            original_idref="title",
            original_path="OEBPS/title.xhtml",
            href="chapters/item_001.html",
            linear="yes",
            media_type="application/xhtml+xml",
            role="title_page",
            char_count=10,
            first_heading="Title",
            output_strategy="source_only",
        ),
        manifest.SpineEntry(
            id="item_002",
            src_idref="chap01",
            src_href="OEBPS/ch01.xhtml",
            original_idref="chap01",
            original_path="OEBPS/ch01.xhtml",
            href="chapters/item_002.html",
            linear="yes",
            media_type="application/xhtml+xml",
            role="body",
            char_count=100,
            first_heading="Chapter 1",
            output_strategy="translate",
            translation_id="ch_01",
        ),
    ]

    assert manifest.chapters_from_spine(entries) == [
        {
            "id": "ch_01",
            "spine_id": "item_002",
            "href": "chapters/item_002.html",
            "src_href": "OEBPS/ch01.xhtml",
            "original_idref": "chap01",
            "original_path": "OEBPS/ch01.xhtml",
            "char_count": 100,
            "first_heading": "Chapter 1",
            "role": "body",
            "output_strategy": "translate",
        }
    ]


def test_entry_original_path_prefers_original_path():
    entry = {
        "id": "item_001",
        "original_path": "EPUB/text/chapter-1.xhtml",
        "src_href": "EPUB/text/fallback.xhtml",
        "href": "chapters/item_001.html",
    }

    assert manifest.entry_original_path(entry, "EPUB/content.opf") == "EPUB/text/chapter-1.xhtml"


def test_entry_original_path_falls_back_to_opf_dir_and_strips_chapters_prefix():
    entry = {
        "id": "item_002",
        "href": "chapters/item_002.html",
    }

    assert manifest.entry_original_path(entry, "OEBPS/content.opf") == "OEBPS/item_002.xhtml"


def test_load_save_round_trip(tmp_path: Path):
    path = tmp_path / "manifest.json"
    data = {"title": "Tiny", "spine": [], "chapters": []}

    assert manifest.load(path) is None
    manifest.save(data, path)
    assert manifest.load(path) == data

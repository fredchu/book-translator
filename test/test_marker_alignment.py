"""Unit tests for marker_alignment — wrap source + parse translated output."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import marker_alignment as ma  # type: ignore  # noqa: E402


def test_wrap_paragraphs_inserts_sequential_markers():
    paras = ["First para.", "Second para.", "Third para."]
    text = ma.wrap_paragraphs(paras)
    assert "[[PARA_1]]" in text
    assert "[[PARA_2]]" in text
    assert "[[PARA_3]]" in text
    assert text.index("[[PARA_1]]") < text.index("[[PARA_2]]") < text.index("[[PARA_3]]")
    # marker must precede the paragraph it labels
    assert text.index("[[PARA_2]]") < text.index("Second para.")


def test_wrap_paragraphs_handles_single_paragraph():
    paras = ["Only one."]
    text = ma.wrap_paragraphs(paras)
    assert "[[PARA_1]]" in text
    assert "Only one." in text


def test_wrap_paragraphs_empty_input_returns_empty_string():
    assert ma.wrap_paragraphs([]) == ""


def test_parse_marker_output_extracts_translations_in_order():
    output = "[[PARA_1]]\n第一段。\n\n[[PARA_2]]\n第二段。\n\n[[PARA_3]]\n第三段。"
    result = ma.parse_marker_output(output, expected_count=3)
    assert result.translations == ["第一段。", "第二段。", "第三段。"]
    assert result.missing_markers == []
    assert result.extra_markers == []
    assert result.is_aligned is True


def test_parse_marker_output_detects_missing_marker():
    # Subagent dropped PARA_2
    output = "[[PARA_1]]\n第一段。\n\n[[PARA_3]]\n第三段。"
    result = ma.parse_marker_output(output, expected_count=3)
    assert result.is_aligned is False
    assert result.missing_markers == [2]
    # parsed translations for the markers that DID appear
    assert result.translations_by_idx == {1: "第一段。", 3: "第三段。"}


def test_parse_marker_output_detects_extra_marker():
    # Subagent invented PARA_4
    output = "[[PARA_1]]\n第一段。\n\n[[PARA_2]]\n第二段。\n\n[[PARA_4]]\n第四段?"
    result = ma.parse_marker_output(output, expected_count=2)
    assert result.is_aligned is False
    assert result.extra_markers == [4]


def test_parse_marker_output_detects_duplicate_markers():
    output = "[[PARA_1]] x\n\n[[PARA_2]] y\n\n[[PARA_2]] y2\n\n[[PARA_3]] z"
    result = ma.parse_marker_output(output, expected_count=3)
    assert result.duplicate_markers == [2]
    assert result.is_aligned is False
    assert result.translations_by_idx[2] == "y"


def test_parse_marker_output_triple_duplicate_reports_once():
    output = "[[PARA_5]] first\n\n[[PARA_5]] second\n\n[[PARA_5]] third"
    result = ma.parse_marker_output(output, expected_count=5)
    assert result.duplicate_markers == [5]


def test_parse_marker_output_no_duplicates_when_clean():
    output = "[[PARA_1]]\n第一段。\n\n[[PARA_2]]\n第二段。"
    result = ma.parse_marker_output(output, expected_count=2)
    assert result.duplicate_markers == []
    assert result.is_aligned is True


def test_is_aligned_false_when_only_duplicates_present():
    output = "[[PARA_1]] x\n\n[[PARA_2]] y\n\n[[PARA_2]] y2\n\n[[PARA_3]] z"
    result = ma.parse_marker_output(output, expected_count=3)
    assert result.missing_markers == []
    assert result.extra_markers == []
    assert result.duplicate_markers == [2]
    assert result.is_aligned is False


def test_parse_marker_output_tolerates_whitespace_variants():
    output = "[[ PARA_1 ]]\n第一段\n\n[[para_2]]\n第二段"  # space + lowercase
    result = ma.parse_marker_output(output, expected_count=2)
    # we accept tolerant matching
    assert result.is_aligned is True
    assert result.translations == ["第一段", "第二段"]


def test_parse_marker_output_no_markers_at_all():
    output = "整段都沒有 marker"
    result = ma.parse_marker_output(output, expected_count=3)
    assert result.is_aligned is False
    assert result.missing_markers == [1, 2, 3]
    assert result.translations == []

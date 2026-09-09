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


def test_collapse_internal_blank_lines_collapses_run_to_single_newline():
    text, changed = ma.collapse_internal_blank_lines("上半段。\n\n下半段。")
    assert text == "上半段。\n下半段。"
    assert changed is True


def test_collapse_internal_blank_lines_tolerates_trailing_whitespace_on_blank_line():
    text, changed = ma.collapse_internal_blank_lines("上半段。\n   \n下半段。")
    assert text == "上半段。\n下半段。"
    assert changed is True


def test_collapse_internal_blank_lines_noop_on_clean_text():
    text, changed = ma.collapse_internal_blank_lines("單行段落，沒有空行。")
    assert text == "單行段落，沒有空行。"
    assert changed is False


def test_parse_marker_output_collapses_blank_line_inside_one_body():
    """Review-15 §7: a blank line inside PARA_1's own body used to survive
    into `translations`, so "\\n\\n".join() later made 2 source paragraphs
    look like 3 to any downstream consumer that re-splits on "\\n\\n"
    (assemble.py, seam repair, run_benchmark.py)."""
    output = "[[PARA_1]]\n上半。\n\n下半。\n\n[[PARA_2]]\n第二段。"
    result = ma.parse_marker_output(output, expected_count=2)
    assert result.is_aligned is True
    assert result.translations == ["上半。\n下半。", "第二段。"]
    assert result.blank_line_markers == [1]
    # the invariant this whole fix protects: re-joining must not desync
    # paragraph count vs the marker count.
    assert len("\n\n".join(result.translations).split("\n\n")) == 2


def test_parse_marker_output_blank_line_markers_empty_when_clean():
    output = "[[PARA_1]]\n甲\n\n[[PARA_2]]\n乙"
    result = ma.parse_marker_output(output, expected_count=2)
    assert result.blank_line_markers == []


def test_parse_marker_output_blank_line_only_recorded_for_kept_duplicate():
    # PARA_1 appears twice; only the FIRST occurrence is kept (found_by_idx
    # dedup) — blank_line_markers must reflect the kept one, not the discarded one.
    output = "[[PARA_1]]\n乾淨版本\n\n[[PARA_1]]\n重複，含\n\n空行\n\n[[PARA_2]]\n乙"
    result = ma.parse_marker_output(output, expected_count=2)
    assert result.duplicate_markers == [1]
    assert result.blank_line_markers == []  # the kept (first) body has no blank line

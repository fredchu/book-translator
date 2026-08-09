"""Spec §5.3 — per-book term table reaches the offline prompt as lookup data."""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import dispatch  # noqa: E402


def _write(book: Path, payload) -> Path:
    book.mkdir(parents=True, exist_ok=True)
    (book / dispatch.FIXED_TERMS_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return book


def test_absent_file_yields_no_terms_and_unchanged_prompt(tmp_path: Path) -> None:
    dispatch.load_fixed_terms.cache_clear()
    assert dispatch.load_fixed_terms(tmp_path) == {}
    system, _ = dispatch.build_ollama_chunk_prompt(chunk_paragraphs=["Hi."])
    assert "固定譯法" not in system


def test_terms_reach_the_system_prompt(tmp_path: Path) -> None:
    dispatch.load_fixed_terms.cache_clear()
    book = _write(tmp_path / "b1", {"terms": {"private commons": "私人公地"}})
    terms = dispatch.load_fixed_terms(book)
    system, _ = dispatch.build_ollama_chunk_prompt(
        chunk_paragraphs=["Hi."], fixed_terms=terms)
    assert "private commons＝私人公地" in system
    assert "固定譯法，全書一致" in system


def test_bare_mapping_without_terms_key_is_accepted(tmp_path: Path) -> None:
    dispatch.load_fixed_terms.cache_clear()
    book = _write(tmp_path / "b2", {"superagency": "超級能動性"})
    assert dispatch.load_fixed_terms(book) == {"superagency": "超級能動性"}


def test_marker_contract_survives_term_injection(tmp_path: Path) -> None:
    """Terms are lookup data; they must not displace the format contract."""
    dispatch.load_fixed_terms.cache_clear()
    book = _write(tmp_path / "b3", {"terms": {f"term{i}": f"詞{i}" for i in range(20)}})
    system, user = dispatch.build_ollama_chunk_prompt(
        chunk_paragraphs=["A.", "B."], fixed_terms=dispatch.load_fixed_terms(book))
    assert "[[PARA_1]]" in system
    assert "禁止使用任何簡體字" in system
    assert "[[PARA_2]]" in user


def test_empty_and_malformed_entries_are_dropped(tmp_path: Path) -> None:
    dispatch.load_fixed_terms.cache_clear()
    book = _write(tmp_path / "b4", {"terms": {"ok": "好", "": "空鍵", "空值": ""}})
    assert dispatch.load_fixed_terms(book) == {"ok": "好"}

"""Unit tests for the paragraph chunker."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import chunker as chk  # type: ignore  # noqa: E402


def test_empty_input_returns_empty_plan():
    plan = chk.chunk_paragraphs([])
    assert plan.chunks == []
    assert plan.total_paragraphs == 0


def test_single_short_paragraph_one_chunk():
    plan = chk.chunk_paragraphs(["Hello."], max_chars=3000)
    assert len(plan.chunks) == 1
    assert plan.chunks[0].paragraphs == ("Hello.",)
    assert plan.chunks[0].start_idx == 0
    assert plan.chunks[0].end_idx == 1


def test_paragraphs_fit_in_one_chunk():
    paras = ["a" * 100, "b" * 100, "c" * 100]
    plan = chk.chunk_paragraphs(paras, max_chars=3000)
    assert len(plan.chunks) == 1
    assert plan.chunks[0].paragraphs == tuple(paras)


def test_overflow_creates_new_chunk_at_paragraph_boundary():
    paras = ["a" * 1500, "b" * 1500, "c" * 100]
    plan = chk.chunk_paragraphs(paras, max_chars=2000)
    # First chunk takes paragraph 0 (1500 chars). Adding p1 (1500) would
    # exceed 2000; flush, start new chunk with p1+p2.
    assert len(plan.chunks) == 2
    assert plan.chunks[0].paragraphs == (paras[0],)
    assert plan.chunks[0].start_idx == 0
    assert plan.chunks[1].paragraphs == (paras[1], paras[2])
    assert plan.chunks[1].start_idx == 1
    assert plan.chunks[1].end_idx == 3


def test_oversized_single_paragraph_gets_its_own_chunk():
    # A single paragraph longer than max_chars must still emit as one chunk
    # (we don't sub-split paragraphs).
    paras = ["x" * 5000]
    plan = chk.chunk_paragraphs(paras, max_chars=1000)
    assert len(plan.chunks) == 1
    assert plan.chunks[0].paragraphs == (paras[0],)


def test_total_paragraphs_roundtrips():
    paras = [f"para {i}" * 200 for i in range(20)]
    plan = chk.chunk_paragraphs(paras, max_chars=3000)
    assert plan.total_paragraphs == 20
    # Every source paragraph appears exactly once in order
    flat = [p for c in plan.chunks for p in c.paragraphs]
    assert flat == paras


def test_start_idx_chain_is_contiguous():
    paras = [f"P{i}" * 800 for i in range(10)]  # each ~1600 chars
    plan = chk.chunk_paragraphs(paras, max_chars=3000)
    assert plan.chunks[0].start_idx == 0
    for prev, nxt in zip(plan.chunks, plan.chunks[1:]):
        assert nxt.start_idx == prev.end_idx


def test_char_count_per_chunk():
    paras = ["a" * 100, "b" * 200, "c" * 300]
    plan = chk.chunk_paragraphs(paras, max_chars=3000)
    assert plan.chunks[0].char_count == 600


def test_stitch_joins_with_blank_lines():
    chunks = ["chunk one text", "chunk two text", "chunk three text"]
    assert chk.stitch(chunks) == "chunk one text\n\nchunk two text\n\nchunk three text"


def test_stitch_skips_empty_chunks():
    chunks = ["alpha", "", "  ", "beta"]
    assert chk.stitch(chunks) == "alpha\n\nbeta"


def test_stitch_strips_whitespace_at_chunk_edges():
    chunks = ["  alpha\n", "\nbeta  "]
    assert chk.stitch(chunks) == "alpha\n\nbeta"


def test_max_paragraphs_caps_chunk_size_even_under_char_budget():
    # 60 tiny references each 20 chars (total 1200 chars), well under
    # max_chars 3000 — but should still split because max_paragraphs=20.
    paras = ["ref" * 6 for _ in range(60)]  # 18 chars each = 1080 chars total
    plan = chk.chunk_paragraphs(paras, max_chars=3000, max_paragraphs=20)
    assert len(plan.chunks) == 3
    for c in plan.chunks:
        assert len(c.paragraphs) == 20


def test_max_paragraphs_default_is_twenty():
    paras = ["x"] * 25
    plan = chk.chunk_paragraphs(paras, max_chars=10000)
    assert len(plan.chunks) == 2
    assert len(plan.chunks[0].paragraphs) == 20
    assert len(plan.chunks[1].paragraphs) == 5

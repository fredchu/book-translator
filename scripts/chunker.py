"""Paragraph chunker for within-chapter translation.

Splits a chapter's paragraphs into translation-sized chunks (~1500-token budget)
so a marker-aligned prompt fits comfortably in a model's `num_predict` window.
Borrowed from Bocky's bilingual-book-translator design — `[[PARA_N]]` markers
combined with small chunks keep instruction-following reliable for local
models (translategemma / hy-mt2) where whole-chapter prompts hit ~50% marker
misalignment rate.

Boundary discipline:
- Never split a paragraph mid-sentence. Each chunk is N whole paragraphs.
- Markers inside each chunk are *locally* numbered `[[PARA_1]]..[[PARA_N]]`.
  Local numbering is more reliable than global indexing (model echoes 1..N
  every time, instead of needing to remember it's at global 73..91). Callers
  stitch chunks back in order, recovering global ordering implicitly.

Token estimation is char-based for simplicity (tiktoken doesn't match
Hy-MT2 / Gemma tokenizers well). Default budget assumes English source +
Traditional Chinese output:
- 1 English char ≈ 0.3 tokens
- 1 Chinese char ≈ 1.5 tokens
- 1500 tokens of source budget ≈ 5000 English chars
- 1500 tokens of output budget ≈ 1000 Chinese chars
- Combined budget per chunk ≈ 3000 source chars to leave headroom for
  prompt overhead + Chinese output expansion.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_CHARS_PER_CHUNK = 3000
DEFAULT_MAX_PARAGRAPHS_PER_CHUNK = 20


@dataclass(frozen=True)
class Chunk:
    """A contiguous run of paragraphs from a chapter."""

    paragraphs: tuple[str, ...]
    start_idx: int  # 0-indexed; first paragraph's position in the original chapter

    @property
    def end_idx(self) -> int:
        """Exclusive end index in the original chapter."""
        return self.start_idx + len(self.paragraphs)

    @property
    def char_count(self) -> int:
        return sum(len(p) for p in self.paragraphs)


@dataclass
class ChunkPlan:
    chunks: list[Chunk] = field(default_factory=list)

    @property
    def total_paragraphs(self) -> int:
        return sum(len(c.paragraphs) for c in self.chunks)


def chunk_paragraphs(
    paragraphs: list[str],
    *,
    max_chars: int = DEFAULT_CHARS_PER_CHUNK,
    max_paragraphs: int = DEFAULT_MAX_PARAGRAPHS_PER_CHUNK,
) -> ChunkPlan:
    """Pack paragraphs into chunks bounded by BOTH max_chars and max_paragraphs.

    Marker-aligned prompts strain on two axes: total tokens (covered by
    max_chars) and marker count (covered by max_paragraphs). A reference list
    might pack 100+ tiny one-line citations under the char budget but the
    model can't reliably echo 100 markers — so we cap paragraph count too.

    Each chunk is at least one paragraph (an oversized single paragraph
    becomes its own chunk; we don't sub-split paragraphs).
    """
    plan = ChunkPlan()
    current: list[str] = []
    current_chars = 0
    start_idx = 0

    for i, p in enumerate(paragraphs):
        plen = len(p)
        would_overflow_chars = current_chars + plen > max_chars
        would_overflow_count = len(current) >= max_paragraphs
        if current and (would_overflow_chars or would_overflow_count):
            plan.chunks.append(Chunk(paragraphs=tuple(current), start_idx=start_idx))
            current = []
            current_chars = 0
            start_idx = i
        current.append(p)
        current_chars += plen

    if current:
        plan.chunks.append(Chunk(paragraphs=tuple(current), start_idx=start_idx))

    return plan


def stitch(chunk_translations: list[str]) -> str:
    """Join translated chunks back into a chapter translation.

    Each chunk_translation is the already-extracted plain text (markers
    stripped, paragraphs separated by blank lines). Stitching is plain
    blank-line concatenation; order is preserved by the caller's list order.
    """
    cleaned = [c.strip() for c in chunk_translations if c.strip()]
    return "\n\n".join(cleaned)

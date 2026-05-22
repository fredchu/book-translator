"""Paragraph marker alignment for subagent translation prompts.

Wraps source paragraphs with [[PARA_N]] markers so the subagent's output can be
parsed deterministically back to per-paragraph translations. Borrowed from Bocky's
bilingual-book-translator design — the marker contract gives us structural
verification (input N markers, output N markers) instead of fragile paragraph-
count comparison via blank-line splitting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_MARKER_RE = re.compile(r"\[\[\s*PARA[\s_]*(\d+)\s*\]\]", re.IGNORECASE)


@dataclass
class ParseResult:
    """Result of parsing a marker-aligned subagent response."""

    translations: list[str]  # in marker order, only those that aligned
    translations_by_idx: dict[int, str]  # 1-indexed marker number -> translation
    missing_markers: list[int]  # marker numbers expected but absent
    extra_markers: list[int]  # marker numbers present but unexpected
    is_aligned: bool = field(init=False)

    def __post_init__(self) -> None:
        self.is_aligned = not self.missing_markers and not self.extra_markers


def wrap_paragraphs(paragraphs: list[str]) -> str:
    """Wrap each paragraph with `[[PARA_N]]` (1-indexed)."""
    if not paragraphs:
        return ""
    parts: list[str] = []
    for i, p in enumerate(paragraphs, start=1):
        parts.append(f"[[PARA_{i}]]\n{p}")
    return "\n\n".join(parts)


def parse_marker_output(output: str, expected_count: int) -> ParseResult:
    """Parse a marker-aligned response back to per-paragraph translations.

    Tolerates:
      - lowercase / whitespace variations inside the marker
      - extra blank lines between marker blocks
      - leading/trailing prose around markers
    """
    if not output:
        return ParseResult(
            translations=[],
            translations_by_idx={},
            missing_markers=list(range(1, expected_count + 1)),
            extra_markers=[],
        )

    matches = list(_MARKER_RE.finditer(output))
    if not matches:
        return ParseResult(
            translations=[],
            translations_by_idx={},
            missing_markers=list(range(1, expected_count + 1)),
            extra_markers=[],
        )

    found_by_idx: dict[int, str] = {}
    for i, m in enumerate(matches):
        idx = int(m.group(1))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(output)
        body = output[start:end].strip()
        found_by_idx[idx] = body

    expected = set(range(1, expected_count + 1))
    found = set(found_by_idx.keys())
    missing = sorted(expected - found)
    extra = sorted(found - expected)

    # ordered translations for indices we did find, in 1..N order
    ordered = [found_by_idx[i] for i in sorted(found_by_idx) if i in expected]

    return ParseResult(
        translations=ordered,
        translations_by_idx=found_by_idx,
        missing_markers=missing,
        extra_markers=extra,
    )

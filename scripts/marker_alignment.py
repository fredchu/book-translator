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

# A marker's body is meant to be exactly one paragraph. Every downstream
# consumer (assemble.py, seam repair, run_benchmark.py) rejoins bodies with
# "\n\n" and later re-splits on "\n\n" to recover paragraph boundaries — a
# blank line the model happens to insert inside its own body is
# indistinguishable from a real boundary once joined, and silently desyncs
# paragraph counts (source N paragraphs -> N+1 in the assembled output).
_INTERNAL_BLANK_LINE_RE = re.compile(r"\n[ \t]*\n+")


def collapse_internal_blank_lines(text: str) -> tuple[str, bool]:
    """Collapse any blank-line run inside one paragraph's text to a single
    newline. Returns (possibly-modified text, whether anything changed).

    Used by parse_marker_output() for marker bodies, and directly by the
    no-marker single-paragraph fallback prompt in translate_book_ollama.py
    (which never goes through the marker parser at all) — both paths produce
    text that other code treats as exactly one paragraph.
    """
    collapsed = _INTERNAL_BLANK_LINE_RE.sub("\n", text)
    return collapsed, collapsed != text


@dataclass
class ParseResult:
    """Result of parsing a marker-aligned subagent response."""

    translations: list[str]  # in marker order, only those that aligned
    translations_by_idx: dict[int, str]  # 1-indexed marker number -> translation
    missing_markers: list[int]  # marker numbers expected but absent
    extra_markers: list[int]  # marker numbers present but unexpected
    duplicate_markers: list[int]  # marker numbers that appeared more than once
    blank_line_markers: list[int] = field(default_factory=list)  # markers whose
    # body contained an internal blank line, collapsed before being stored —
    # reported so callers can surface a quality warning, not to gate/retry.
    is_aligned: bool = field(init=False)

    def __post_init__(self) -> None:
        self.is_aligned = (
            not self.missing_markers
            and not self.extra_markers
            and not self.duplicate_markers
        )


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
            duplicate_markers=[],
        )

    matches = list(_MARKER_RE.finditer(output))
    if not matches:
        return ParseResult(
            translations=[],
            translations_by_idx={},
            missing_markers=list(range(1, expected_count + 1)),
            extra_markers=[],
            duplicate_markers=[],
        )

    found_by_idx: dict[int, str] = {}
    marker_counts: dict[int, int] = {}
    blank_line_markers: list[int] = []
    for i, m in enumerate(matches):
        idx = int(m.group(1))
        marker_counts[idx] = marker_counts.get(idx, 0) + 1
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(output)
        body = output[start:end].strip()
        body, had_blank_line = collapse_internal_blank_lines(body)
        if idx not in found_by_idx:
            found_by_idx[idx] = body
            if had_blank_line:
                blank_line_markers.append(idx)

    expected = set(range(1, expected_count + 1))
    found = set(found_by_idx.keys())
    missing = sorted(expected - found)
    extra = sorted(found - expected)
    duplicate = sorted(idx for idx, count in marker_counts.items() if count > 1)

    # ordered translations for indices we did find, in 1..N order
    ordered = [found_by_idx[i] for i in sorted(found_by_idx) if i in expected]

    return ParseResult(
        translations=ordered,
        translations_by_idx=found_by_idx,
        missing_markers=missing,
        extra_markers=extra,
        duplicate_markers=duplicate,
        blank_line_markers=sorted(blank_line_markers),
    )

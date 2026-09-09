"""Shared source-term matching used by term building and prompt injection."""

from __future__ import annotations

import functools
import re
import unicodedata

_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'"})
_HYPHENS = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-"})


def normalize_match_text_with_map(text: str) -> tuple[str, list[int]]:
    """Return normalized text plus each output character's source index."""
    output, source_indexes = [], []
    for index, original in enumerate(text):
        translated = original.translate(_APOSTROPHES).translate(_HYPHENS)
        for char in unicodedata.normalize("NFKD", translated):
            if not unicodedata.combining(char):
                output.append(char)
                source_indexes.append(index)
    return "".join(output), source_indexes


def normalize_match_text(text: str) -> str:
    """Normalize matching variants without changing the stored source key."""
    return normalize_match_text_with_map(text)[0]


@functools.lru_cache(maxsize=8192)
def _normalized_pattern(term: str) -> re.Pattern[str]:
    normalized = normalize_match_text(term)
    return re.compile(
        rf"(?<![A-Za-z0-9]){re.escape(normalized)}(?![A-Za-z0-9])",
        re.IGNORECASE,
    )


def iter_term_matches_normalized(term: str, normalized_text: str):
    """Yield matches in text already processed by :func:`normalize_match_text`."""
    if not term or not normalized_text:
        return
    normalized_term = normalize_match_text(term)
    capitalized = term[:1].isupper()
    for match in _normalized_pattern(term).finditer(normalized_text):
        matched = match.group(0)
        if capitalized and matched != normalized_term and not matched.isupper():
            continue
        yield match


def iter_term_matches(term: str, text: str):
    """Yield normalized-text matches under injection's case and boundary rules.

    Capitalized keys remain case-sensitive, except an ALL-CAPS source spelling
    may match a Title Case key. Lowercase terms match sentence-initial capitals.
    """
    if not term or not text:
        return
    yield from iter_term_matches_normalized(term, normalize_match_text(text))


def term_occurs_normalized(term: str, normalized_text: str) -> bool:
    return next(iter_term_matches_normalized(term, normalized_text), None) is not None


def term_occurs(term: str, text: str) -> bool:
    return term_occurs_normalized(term, normalize_match_text(text))


def count_term_occurrences_normalized(term: str, normalized_text: str) -> int:
    return sum(1 for _ in iter_term_matches_normalized(term, normalized_text))


def count_term_occurrences(term: str, text: str) -> int:
    return count_term_occurrences_normalized(term, normalize_match_text(text))

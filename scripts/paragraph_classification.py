"""Paragraph-level classification shared by assembly and coverage audits.

These predicates never decide whether a source paragraph is sent for translation.
They only explain why an already non-Chinese result may legitimately remain so.
"""

from __future__ import annotations

import re
import sys
from pathlib import PurePosixPath

HAN_RE = re.compile(r"[\u4e00-\u9fff]")
_ASCII_LETTER_RE = re.compile(r"[A-Za-z]")
_IDENTIFIER_RE = re.compile(r"[._A-Za-z0-9-]{6,}")
_BARE_NUMBER_RE = re.compile(r"(?:[0-9]{1,4}|[IVXLCDM]{1,8})")
_URL_OR_EMAIL_RE = re.compile(r"(?:https?://|www\.|\S+@\S+)", re.IGNORECASE)


# The length the coverage universe has always used. `bilingual_coverage_audit`
# reads it from here so the "what counts as English content" threshold and the
# "what is newly in scope, therefore amber" threshold cannot drift apart.
LEGACY_ENGLISH_MIN_LENGTH = 50


def is_english_content(text: str) -> bool:
    """Whether coverage policy requires an adjacent Han translation."""
    if len(text) < LEGACY_ENGLISH_MIN_LENGTH or HAN_RE.search(text):
        return False
    letters = len(_ASCII_LETTER_RE.findall(text))
    return letters >= max(20, int(len(text) * 0.35))


# Page names carry a publisher prefix and a numeric one ("86_Bibliography",
# "Superagency_Notes", "endnotes_split_006"), so the role has to be read as a
# whole token rather than a substring. Substring matching produced four
# false exemptions, every one of them in the direction of weakening the gate:
# "preferences" contains "reference", a book's opening "index.xhtml" is not an
# index, "notes_on_contributors" is not endnotes, and `stem == "b05"` was one
# book's filename hardcoded into a shared classifier.
_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+|(?<=[a-z])(?=[0-9])|(?<=[0-9])(?=[a-z])")
_PREPOSITIONS = ("on", "about", "for", "to", "from", "with")


def _page_tokens(stem: str) -> set[str]:
    return {token for token in _TOKEN_SPLIT_RE.split(stem) if token}


def _has_token(tokens: set[str], wanted: tuple[str, ...]) -> bool:
    return any(token in tokens for token in wanted)


def untranslated_reason(path: str, node, text: str) -> str | None:
    """Return a reason for an already non-Han paragraph, never a skip decision.

    Position (page filename or publisher CSS class) is primary. Content-only
    identifier/non-Latin checks are deliberately narrow and are evaluated only
    after translation has produced no Han characters.
    """
    page = PurePosixPath(path).name.casefold()
    stem = PurePosixPath(path).stem.casefold()
    classes = {str(value).casefold() for value in node.get("class", [])}

    tokens = _page_tokens(stem)

    if _has_token(tokens, ("bibliography", "bibliographies", "references")) or any(
        cls == "bib" or cls.startswith("bib-") for cls in classes
    ):
        return "bibliography"
    if (_has_token(tokens, ("index", "indexes")) and stem != "index") or any(
        re.fullmatch(r"index\d*", cls) or re.fullmatch(r"ind\d+a?", cls) for cls in classes
    ):
        # `index.xhtml` alone is a book's opening page, the way index.html is on
        # a website; a real back-of-book index carries a position prefix
        # ("87_Index"). Every misread here weakens the gate, so require the
        # extra token.
        return "index"
    if _has_token(tokens, ("notes", "endnotes", "endnote", "footnotes")) and not _has_token(
        tokens, _PREPOSITIONS
    ):
        # "notes_on_contributors" is a prose page about people, not endnotes.
        # A preposition means the word "notes" is describing something else.
        return "notes"
    if _has_token(tokens, ("copyright", "publisher", "praise")):
        return "publication_metadata"

    stripped = text.strip()
    if _BARE_NUMBER_RE.fullmatch(stripped):
        return "identifier"
    if _URL_OR_EMAIL_RE.fullmatch(stripped) or (
        _IDENTIFIER_RE.fullmatch(stripped)
        and ("_" in stripped or any(char.isdigit() for char in stripped))
    ):
        return "identifier"
    if not _ASCII_LETTER_RE.search(stripped) and not HAN_RE.search(stripped):
        return "non_latin"
    return None


_WARNED_LEGACY_SOURCE_ONLY = False


def reasoned_source_only_entries(data) -> dict[str, str]:
    """Accept only explicit ``src_text`` + non-empty ``reason`` exceptions.

    Bare-string entries are the pre-2026-09-09 format, written when the rule was
    "no zh sibling means exempt". They are rejected on purpose — that rule is
    what let 506 echoed/misaligned paragraphs through — but rejecting them
    silently would make an old exemption list vanish with no signal, so say so
    once.
    """
    global _WARNED_LEGACY_SOURCE_ONLY
    result: dict[str, str] = {}
    if not isinstance(data, list):
        return result
    dropped = 0
    for item in data:
        if not isinstance(item, dict):
            dropped += 1
            continue
        src_text = item.get("src_text")
        reason = item.get("reason")
        if isinstance(src_text, str) and isinstance(reason, str) and reason.strip():
            result[src_text] = reason.strip()
        else:
            dropped += 1
    if dropped and not _WARNED_LEGACY_SOURCE_ONLY:
        _WARNED_LEGACY_SOURCE_ONLY = True
        print(
            f"[warn] source_only.json: ignored {dropped} entr"
            f"{'y' if dropped == 1 else 'ies'} without an explicit reason "
            "(pre-2026-09-09 bare-string format). Re-assemble to regenerate it.",
            file=sys.stderr,
        )
    return result

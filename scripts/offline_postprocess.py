"""Offline-path post-processing: Simplified->Traditional, character-name
coherence, and bilingual nav labels.

The offline driver (translate_book_ollama.py) skips the main-session glossary /
nav-override build for speed, so these deterministic passes recover the quality
those steps would have provided:

1. ``to_traditional`` — opencc s2twp; fixes the model's residual Simplified leak
   and normalises to Taiwan character forms. Soft dependency: a no-op (with one
   warning) when opencc is unavailable.
2. ``normalize_character_names`` — without a glossary the model drifts between
   transliteration variants of the same name (瑪德琳 vs 梅德琳). Conservatively
   merges minority variants into the dominant form.
3. ``build_nav_overrides`` — populates translations_extra.json nav_overrides
   from each chapter's translated title so the ToC renders bilingual.
"""

from __future__ import annotations

import collections
import re
import sys
from pathlib import Path

try:  # shared modules live alongside this file
    from . import content_blocks as cb
    from . import translations_extra as te
except ImportError:  # pragma: no cover - script-style import
    import content_blocks as cb  # type: ignore
    import translations_extra as te  # type: ignore

_HAN = "一-鿿"
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

_CC = None  # cached opencc converter; False once we know it is unavailable


def _converter():
    global _CC
    if _CC is False:
        return None
    if _CC is None:
        try:
            from opencc import OpenCC

            _CC = OpenCC("s2twp")
        except Exception:
            _CC = False
            print(
                "[warn] opencc not installed; skipping Simplified->Traditional "
                "conversion (pip install opencc)",
                file=sys.stderr,
            )
            return None
    return _CC


def to_traditional(text: str) -> str:
    """Convert Simplified Chinese to Taiwan Traditional (s2twp). No-op without opencc."""
    if not text:
        return text
    cc = _converter()
    return cc.convert(text) if cc else text


# --- character-name coherence -------------------------------------------------

def _translation_files(book_dir: Path) -> list[Path]:
    return sorted((book_dir / "chapters").glob("item_*_translation.txt"))


def _canonical_name_tokens(text: str) -> set[str]:
    """High-confidence name tokens: parts of a middle-dot full name (A·B)."""
    tokens: set[str] = set()
    for m in re.finditer(rf"([{_HAN}]{{2,4}})·([{_HAN}]{{2,4}})", text):
        tokens.add(m.group(1))
        tokens.add(m.group(2))
    return tokens


def _hamming1_variant_counts(text: str, name: str) -> collections.Counter:
    """Count all-Han windows of len(name) that differ from name by exactly 1 char."""
    length = len(name)
    counts: collections.Counter = collections.Counter()
    for i in range(len(text) - length + 1):
        window = text[i : i + length]
        if window == name:
            continue
        diff = 0
        ok = True
        for a, b in zip(window, name):
            if not ("一" <= a <= "鿿"):
                ok = False
                break
            if a != b:
                diff += 1
        if ok and diff == 1:
            counts[window] += 1
    return counts


def _is_han(ch: str) -> bool:
    return "一" <= ch <= "鿿"


def _freestanding_count(text: str, token: str) -> int:
    """Count token occurrences with at least one non-Han neighbor (or text edge).

    A transliterated name routinely abuts punctuation/quotes somewhere across a
    book, while a substring of a fixed compound (士尼 in 迪士尼, 曼尼 in 曼尼托巴)
    is always flanked by Han on both sides — this filters those out.
    """
    length = len(token)
    n = len(text)
    count = 0
    start = 0
    while True:
        i = text.find(token, start)
        if i < 0:
            break
        left_free = i == 0 or not _is_han(text[i - 1])
        right_free = i + length >= n or not _is_han(text[i + length])
        if left_free or right_free:
            count += 1
        start = i + 1
    return count


# Names shorter than this are not auto-normalised: 2-char variants collide with
# substrings of longer compounds/names too often to merge safely without a glossary.
MIN_NAME_LEN = 3


def normalize_character_names(
    book_dir: Path,
    *,
    min_name: int = 10,
    min_variant: int = 3,
    dominance: int = 4,
) -> list[tuple[str, str, int]]:
    """Merge minority transliteration variants into the dominant canonical name.

    Conservative: a variant is only merged when (a) the canonical name is a
    middle-dot name part of length >= MIN_NAME_LEN appearing >= min_name times,
    (b) the variant occurs free-standing (with a non-Han neighbour) >= min_variant
    times — so substrings of fixed compounds like 曼尼 in 曼尼托巴 are excluded,
    (c) the canonical dominates by >= dominance x, and (d) the variant is not
    itself a canonical name (never merge two real names).

    Returns the list of (variant, canonical, free-standing count) merges applied.
    """
    files = _translation_files(book_dir)
    if not files:
        return []
    text = "\n".join(f.read_text(encoding="utf-8") for f in files)
    canonical = _canonical_name_tokens(text)

    repl: dict[str, str] = {}
    ambiguous: set[str] = set()
    for name in canonical:
        if len(name) < MIN_NAME_LEN:
            continue
        name_count = text.count(name)
        if name_count < min_name:
            continue
        for var in _hamming1_variant_counts(text, name):
            if var in canonical or var in ambiguous:
                continue
            var_count = _freestanding_count(text, var)
            if var_count < min_variant or name_count < dominance * var_count:
                continue
            if var in repl and repl[var] != name:
                # two canonical names both claim this variant -> unsafe, drop it
                del repl[var]
                ambiguous.add(var)
                continue
            repl[var] = name

    if not repl:
        return []

    applied = [(var, repl[var], _freestanding_count(text, var)) for var in repl]
    # longest variants first so a short variant never rewrites inside a long one
    ordered = sorted(repl, key=len, reverse=True)
    for f in files:
        s = original = f.read_text(encoding="utf-8")
        for var in ordered:
            s = s.replace(var, repl[var])
        if s != original:
            f.write_text(s, encoding="utf-8")
    return applied


# --- bilingual nav labels -----------------------------------------------------

def _segments(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8")
    return [s.strip() for s in re.split(r"\n\s*\n", raw) if s.strip()]


def build_nav_overrides(book_dir: Path, manifest: dict) -> int:
    """Set nav_overrides[idref] = translated title for heading-led translate items.

    Only applies when a chapter's first non-header content block is a heading,
    so its translated title is segment 0. Front matter whose first block is prose
    (e.g. part-divider epigraphs) is left to structural-label fallback. Existing
    nav_overrides keys are preserved.
    """
    spine = manifest.get("spine") or manifest.get("chapters") or []
    extra = te.load(book_dir)
    nav = dict(extra.get("nav_overrides") or {})
    added = 0
    for entry in spine:
        if not isinstance(entry, dict) or entry.get("output_strategy") != "translate":
            continue
        idref = str(entry.get("original_idref") or "")
        if not idref or idref in nav:
            continue
        item_id = str(entry.get("id") or "")
        html_path = book_dir / "chapters" / f"{item_id}.html"
        tr_path = book_dir / "chapters" / f"{item_id}_translation.txt"
        if not (html_path.exists() and tr_path.exists()):
            continue
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
        cb.strip_non_content(soup)
        first = next(cb.walk_text_nodes(soup), None)
        if first is None or first.name not in _HEADING_TAGS:
            continue
        segs = _segments(tr_path)
        if not segs:
            continue
        nav[idref] = segs[0]
        added += 1
    if added:
        extra["nav_overrides"] = nav
        te.save(book_dir, extra)
    return added

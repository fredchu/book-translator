"""Offline-path post-processing: Simplified->Traditional, character-name
coherence, and bilingual nav labels.

The offline driver (translate_book_ollama.py) skips the main-session glossary /
nav-override build for speed, so these deterministic passes recover the quality
those steps would have provided:

1. ``to_traditional`` — sentence-gated opencc s2tw; fixes the model's residual
   Simplified leak without running already-Traditional sentences through an
   unsafe converter. Soft dependency: a no-op (with one warning) when opencc is
   unavailable.
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
# Shared with extract_epub.styled_paragraph_title so both sides of a nav
# label (English from the manifest, Chinese from the translation) agree on
# what counts as a title block.
TITLE_BLOCK_MAX_LEN = 60

_CC = None  # cached opencc converter; False once we know it is unavailable
_SIMPLIFIED_TRIGGERS = None  # cached frozenset; False when dictionary unavailable
_SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？；：\n])|(?<=[.!?] )")


# s2tw, NOT s2twp. The trailing "p" adds mainland->Taiwan *vocabulary*
# substitution, which carries a computing-term table (调用->呼叫, 循环->迴圈,
# 窗口->視窗, 数据->資料). Both configs are unsafe on already-Traditional input:
# besides multi-mapping guesses such as 范->範, s2tw can produce 肥皂剧 from
# correct 肥皂劇 and 最多隻能 from 最多只能. Therefore s2tw is applied only to
# sentences containing an unambiguous Simplified trigger character.
_OPENCC_CONFIG = "s2tw"

# STCharacters alone calls these valid Taiwan forms Simplified. 疱 is accepted in
# 教育部's「疱疹」entry; 雇、霉、晒 are in its 4,808 common-character list;
# 苧（苧麻）and 洼（窪地／姓氏）have independent dictionary senses. Keep these
# forms from triggering conversion when they occur in otherwise-Traditional text.
# We deliberately do not exclude the whole 4,808∩trigger-set intersection: it
# includes highly diagnostic Simplified forms such as 么 (什么) and 坏.
_TRIGGER_EXCLUSIONS = frozenset("疱雇霉晒苧洼")
# 着 is absent from STCharacters (it lives in TWVariants), but in this model's
# output it is a known mainland-form residual (穿着/挽着). Do not import the whole
# TWVariants table: it also contains valid Traditional variants such as 羣 and 祕.
_EXTRA_SIMPLIFIED_TRIGGERS = frozenset("着")

# Even in a triggered sentence, s2tw may mis-resolve one-to-many characters such
# as 干. Keep the existing narrow term shields to reduce collateral damage.
_PROTECTED_TERMS = (
    "干擾", "干預", "干涉", "干旱", "干戈", "若干", "干支",
    "排泄", "污染", "污水", "污垢",
)
# Private Use Area placeholders; one per protected term, never in real prose.
_PROTECT_MAP = {t: chr(0xE000 + i) for i, t in enumerate(_PROTECTED_TERMS)}


def _converter():
    global _CC
    if _CC is False:
        return None
    if _CC is None:
        try:
            from opencc import OpenCC

            _CC = OpenCC(_OPENCC_CONFIG)
        except Exception:
            _CC = False
            print(
                "[warn] opencc not installed; skipping Simplified->Traditional "
                "conversion (pip install opencc)",
                file=sys.stderr,
            )
            return None
    return _CC


def _simplified_triggers(cc) -> frozenset[str] | None:
    """Load unambiguous Simplified characters from opencc's STCharacters.

    A key only triggers when it is not one of its own Traditional candidates and
    s2tw actually changes it. This rejects ambiguous, valid Traditional forms
    such as 范 (``范 -> 範 范``), unlike the unsafe ``s2t(text) != text`` test.
    """
    global _SIMPLIFIED_TRIGGERS
    if _SIMPLIFIED_TRIGGERS is False:
        return None
    if _SIMPLIFIED_TRIGGERS is None:
        try:
            import opencc

            dictionary = Path(opencc.__file__).resolve().parent / "dictionary" / "STCharacters.txt"
            triggers: set[str] = set()
            for line in dictionary.read_text(encoding="utf-8").splitlines():
                key, candidates = line.split("\t", 1)
                if key not in candidates.split() and cc.convert(key) != key:
                    triggers.add(key)
            triggers.difference_update(_TRIGGER_EXCLUSIONS)
            triggers.update(_EXTRA_SIMPLIFIED_TRIGGERS)
            _SIMPLIFIED_TRIGGERS = frozenset(triggers)
        except Exception:
            _SIMPLIFIED_TRIGGERS = False
            print(
                "[warn] opencc STCharacters.txt unavailable; skipping unsafe "
                "Simplified->Traditional conversion",
                file=sys.stderr,
            )
            return None
    return _SIMPLIFIED_TRIGGERS


def _convert_triggered_sentence(sentence: str, cc, triggers: frozenset[str]) -> str:
    if not any(ch in triggers for ch in sentence):
        return sentence
    for term, ph in _PROTECT_MAP.items():
        sentence = sentence.replace(term, ph)
    sentence = cc.convert(sentence)
    for term, ph in _PROTECT_MAP.items():
        sentence = sentence.replace(ph, term)
    return sentence


def to_traditional(text: str) -> str:
    """Convert sentences containing clear Simplified Chinese to Taiwan forms.

    Sentences are delimited by ``。！？；：``, newlines, and an ASCII ``.!?``
    followed by a space. Already-Traditional sentences are returned byte-for-byte
    unchanged; triggered sentences use s2tw with the
    existing term shields. No-op if opencc or its STCharacters dictionary is
    unavailable.
    """
    if not text:
        return text
    cc = _converter()
    if not cc:
        return text
    triggers = _simplified_triggers(cc)
    if not triggers:
        return text
    return "".join(
        _convert_triggered_sentence(sentence, cc, triggers)
        for sentence in _SENTENCE_BOUNDARY.split(text)
    )


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


# --- inline source-term glosses -----------------------------------------------

# 中文（English）— a gloss only counts when Chinese text precedes the bracket, so
# parenthesised English that is part of the sentence (（see Chapter 3）) is left
# alone. The preceding character is often closing punctuation rather than a Han
# character — 《麻省理工科技評論》（MIT Technology Review）、「大他者」（Big Other）
# — and an earlier Han-only version silently missed a third of all glosses.
_INLINE_GLOSS = re.compile(r"(?<=[一-鿿》」』〉〕】…])（([A-Za-z][^）]{0,60})）")


def dedupe_inline_glosses(book_dir: Path) -> list[tuple[str, int]]:
    """Keep only the first 「中譯（English）」 gloss per term across the whole book.

    OFFLINE_STYLE_RULES asks for a gloss on first mention, but the model sees
    one chunk at a time and has no memory of earlier chunks, so a term is
    re-glossed in every chunk that mentions it (measured: 104 glosses for 78
    unique terms in one chapter). Chapter files are processed in sorted order,
    so "first" means first in reading order.

    Returns (term, removals) for every term that was glossed more than once.
    """
    files = _translation_files(book_dir)
    if not files:
        return []
    seen: set[str] = set()
    dropped: collections.Counter = collections.Counter()

    def _sub(match: re.Match) -> str:
        term = match.group(1).strip()
        key = term.casefold()
        if key in seen:
            dropped[term] += 1
            return ""
        seen.add(key)
        return match.group(0)

    for path in files:
        original = path.read_text(encoding="utf-8")
        updated = _INLINE_GLOSS.sub(_sub, original)
        if updated != original:
            path.write_text(updated, encoding="utf-8")
    return sorted(dropped.items(), key=lambda kv: (-kv[1], kv[0]))


# Acronyms that stay in Latin script after their first glossed mention. The spec
# (§5.2) wants 「人工智慧（AI）」 once, then bare 「AI」 — writing 人工智慧 forty times
# reads worse than the acronym, which is already idiomatic in Chinese tech prose.
ACRONYM_KEEP = ("AI", "LLM", "GPT", "RLHF", "AGI", "API", "GDPR", "CEO", "GPS")

# Chinese has no word delimiters, so a greedy match before （AI） swallows the
# preceding clause: 「隨著高度能動的人工智慧（AI）」 yields 隨著高度能動的人工智慧
# rather than 人工智慧, and replacing that long string matches nothing. Walking
# left from the bracket and stopping at a function word recovers the term.
# Measured 2026-08-09 on the full Superagency run: without this, only 1 of 241
# 人工智慧 mentions collapsed.
# Deliberately narrow. Characters that also occur INSIDE terms must stay out:
# 用 (通用), 能 (智能), 有 (所有), 為 (行為), 向 (向量), 使 (使用者), 對 (對話式),
# 過 (超過), 同 (同義). An over-wide set trimmed 人工通用智慧 down to 智慧 and
# 人工智能 down to nothing.
_TERM_STOP_CHARS = set("的地得了這那些而但若則之是在和與及把被讓從跟由")


def _trim_to_term(candidate: str) -> str:
    """Trim a greedy pre-bracket match down to the term itself."""
    cut = 0
    for i in range(len(candidate) - 1, -1, -1):
        if candidate[i] in _TERM_STOP_CHARS:
            cut = i + 1
            break
    return candidate[cut:]


def collapse_acronym_glosses(book_dir: Path) -> list[tuple[str, str, int]]:
    """After the first 「中譯（ACRONYM）」, replace later 中譯 with the bare acronym.

    Same cross-chunk problem as dedupe_inline_glosses: the model treats every
    chunk as the term's first mention, so a book ends up with the Chinese
    rendering repeated throughout (measured: 人工智慧 x10 in one chapter, in both
    the shipped and the spec-run output). The Chinese rendering is learned from
    the first gloss rather than hardcoded, so a book that glosses 「大型語言模型
    （LLM）」 collapses to LLM without a table entry.

    Runs AFTER dedupe_inline_glosses, which has already reduced each term to a
    single gloss. Returns (chinese, acronym, replacements) per collapsed term.
    """
    files = _translation_files(book_dir)
    if not files:
        return []
    # learn 中譯 for each acronym from its surviving gloss, in reading order
    zh_for: dict[str, str] = {}
    gloss_re = re.compile(
        r"([一-鿿]{2,14})（(" + "|".join(ACRONYM_KEEP) + r")）")
    for path in files:
        for m in gloss_re.finditer(path.read_text(encoding="utf-8")):
            zh_for.setdefault(m.group(2), _trim_to_term(m.group(1)))
    zh_for = {a: z for a, z in zh_for.items() if len(z) >= 2}
    if not zh_for:
        return []

    counts: collections.Counter = collections.Counter()
    for path in files:
        original = text = path.read_text(encoding="utf-8")
        for acro, zh in zh_for.items():
            keep = f"{zh}（{acro}）"
            placeholder = f"\x00{acro}\x00"
            # protect the single surviving gloss, collapse the rest, restore
            text = text.replace(keep, placeholder, 1)
            if zh in text:
                counts[acro] += text.count(zh)
                text = text.replace(zh, acro)
            text = text.replace(placeholder, keep, 1)
        if text != original:
            path.write_text(text, encoding="utf-8")
    return sorted(((zh_for[a], a, n) for a, n in counts.items()),
                  key=lambda t: -t[2])


# --- bilingual nav labels -----------------------------------------------------

def _segments(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8")
    return [s.strip() for s in re.split(r"\n\s*\n", raw) if s.strip()]


def _leading_title_block_count(soup) -> int:
    """How many leading blocks read as a title: 0 (none), 1, or 2.

    A heading tag counts on its own. Publishers that style titles as `<p
    class="CN">CHAPTER 4</p>` + `<p class="CT">THE TRIUMPH…</p>` also count, but
    only when the text is short and ALL CAPS — that requirement is what keeps
    epigraphs and dedications from being mistaken for chapter titles. See
    `extract_epub.styled_paragraph_title` for the corpus measurements behind it.
    """
    count = 0
    for node in cb.walk_text_nodes(soup):
        text = node.get_text(" ", strip=True)
        if not text:
            continue
        is_title = node.name in _HEADING_TAGS or (
            len(text) <= TITLE_BLOCK_MAX_LEN and text.isupper()
        )
        if not is_title:
            break
        count += 1
        if count == 2:
            break
    return count


def build_nav_overrides(book_dir: Path, manifest: dict) -> int:
    """Set nav_overrides[idref] = translated title for title-led translate items.

    Applies when a chapter opens with a title block: either a heading tag, or the
    styled-`<p>` shape publishers use instead (short + ALL CAPS). Front matter
    whose first block is prose (part-divider epigraphs, dedications) is left to
    structural-label fallback. Existing nav_overrides keys are preserved.
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
        title_blocks = _leading_title_block_count(soup)
        if not title_blocks:
            continue
        segs = _segments(tr_path)
        if not segs:
            continue
        # Two title blocks (chapter number + title) become one label so the ToC
        # reads 第四章：私人公地的勝利 rather than just 第四章.
        if title_blocks > 1 and len(segs) > 1 and len(segs[0]) <= 12 and len(segs[1]) <= 40:
            nav[idref] = f"{segs[0]}：{segs[1]}"
        else:
            nav[idref] = segs[0]
        added += 1
    if added:
        extra["nav_overrides"] = nav
        te.save(book_dir, extra)
    return added


def _extract_header_title(html_path: Path) -> str:
    """Chapter display title from the source ``<header>`` (chapter-number heading
    plus ``role="doc-subtitle"`` title), falling back to the first heading.

    ``build_nav_overrides`` cannot reach these because ``strip_non_content`` drops
    the ``<header>`` wrapper, so the chapter title is neither translated nor visible
    as the first walked node. Returns "" when no title text is present.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    parts: list[str] = []
    header = soup.find("header")
    if header is not None:
        for el in header.find_all(sorted(_HEADING_TAGS)):
            text = el.get_text(" ", strip=True)
            if text:
                parts.append(text)
        subtitle = header.find(attrs={"role": "doc-subtitle"})
        if subtitle is not None:
            text = subtitle.get_text(" ", strip=True)
            if text and text not in parts:
                parts.append(text)
    if not parts:
        heading = soup.find(sorted(_HEADING_TAGS))
        if heading is not None:
            text = heading.get_text(" ", strip=True)
            if text:
                parts.append(text)
    return " : ".join(parts)


def translate_header_titles(book_dir: Path, manifest: dict, provider) -> int:
    """Fill nav_overrides for translate items whose title lives in a ``<header>``.

    Complements ``build_nav_overrides`` (which only handles items whose first body
    block is a translated heading). Chapter titles in a ``<header>`` are batch-
    translated through ``provider`` in a single marker-tagged call, then written to
    nav_overrides so both the EPUB nav and the in-body promoted heading render
    bilingually. No-op for items already in nav_overrides or when no provider /
    titles are available. Returns the number of nav_overrides added.
    """
    if provider is None:
        return 0
    spine = manifest.get("spine") or manifest.get("chapters") or []
    extra = te.load(book_dir)
    nav = dict(extra.get("nav_overrides") or {})
    pending: list[tuple[str, str]] = []
    for entry in spine:
        if not isinstance(entry, dict) or entry.get("output_strategy") != "translate":
            continue
        idref = str(entry.get("original_idref") or "")
        if not idref or idref in nav:
            continue
        item_id = str(entry.get("id") or "")
        html_path = book_dir / "chapters" / f"{item_id}.html"
        if not html_path.exists():
            continue
        title = _extract_header_title(html_path)
        if title:
            pending.append((idref, title))
    if not pending:
        return 0
    marked = "\n".join(f"[[T{i + 1}]] {title}" for i, (_, title) in enumerate(pending))
    prompt = (
        "Translate each book chapter/section title into 台灣繁體中文 "
        "(Taiwan Traditional Chinese).\n"
        "Rules: render 'Chapter N' as '第N章', 'Conclusion' as '結論', "
        "'Introduction' as '導論', 'Index' as '索引'; translate the subtitle after "
        "the colon faithfully and concisely; keep the format '第N章：中文副標'.\n"
        "Echo every marker exactly, one per line, then the translation:\n"
        "[[T1]] <translation>\n[[T2]] <translation>\nOutput only the marker lines.\n\n"
        + marked
    )
    try:
        result = provider.translate(
            prompt,
            request_id="nav_titles",
            system=(
                "You are Qwen, created by Alibaba Cloud. You are a helpful assistant.\n"
                "<|think_off|>"
            ),
        )
        raw = getattr(result, "raw_text", "") or ""
    except Exception as exc:  # provider down / signature mismatch — leave to fallback labels
        print(f"[postprocess] header-title translation skipped: {exc}", file=sys.stderr)
        return 0

    zh_by_index: dict[int, str] = {}
    for match in re.finditer(r"\[\[T(\d+)\]\]\s*(.+)", raw):
        idx = int(match.group(1)) - 1
        if 0 <= idx < len(pending):
            zh_by_index[idx] = match.group(2).strip()
    if not zh_by_index:
        # Model dropped the markers but kept order + count: map positionally.
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if len(lines) == len(pending):
            zh_by_index = {i: re.sub(r"^\[\[T\d+\]\]\s*", "", ln) for i, ln in enumerate(lines)}

    added = 0
    for idx, (idref, _english) in enumerate(pending):
        zh = to_traditional(zh_by_index.get(idx, "").strip())
        if zh:
            nav[idref] = zh
            added += 1
    if added:
        extra["nav_overrides"] = nav
        te.save(book_dir, extra)
    return added

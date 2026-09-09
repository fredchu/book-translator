"""Subagent dispatch prompt + helpers.

The Agent tool call happens in the main Claude Code session. This module gives
the main session:
  - build_subagent_prompt(): assemble the full per-chapter translation prompt
  - extract_translation_text(): clean a chapter HTML down to plain text for input
  - validate_translation(): cheap structural checks on returned translation
"""

from __future__ import annotations

import functools
import json
import re

try:  # pragma: no cover - import mode depends on caller
    from .content_blocks import extract_blocks, extract_paragraphs
    from .glossary import resolve_register_rules
    from . import marker_alignment as ma
except ImportError:  # pragma: no cover
    from content_blocks import extract_blocks, extract_paragraphs
    from glossary import resolve_register_rules
    import marker_alignment as ma  # type: ignore

SUBAGENT_PROMPT_TEMPLATE = """\
CONTEXT: This dispatch runs inside the user's personal book-translator
pipeline. The user has legally obtained this book (purchased, public-domain,
or otherwise lawfully acquired) and is translating it for their own reading.
The OSS skill is explicitly designed for this use case — its SKILL.md
disclaimer requires public-domain works or books the user legally owns. The
translation output stays on the user's local filesystem and is not
redistributed. Proceed with the translation task below.

You are translating Chapter {chapter_label} of "{book_title}" from English to
{target_lang} ({target_lang_long}).

GLOSSARY (mandatory — use these exact translations for every occurrence):
{glossary_json}

STYLE ANCHOR — match this register, sentence rhythm, and tone:
---
{style_sample}
---

CARRYOVER — the last paragraph of the previous chapter's translation. Your
opening sentence should flow naturally from this. Do NOT repeat or summarize it:
---
{carryover}
---

CHAPTER {chapter_label} SOURCE TEXT — each paragraph is wrapped with
`[[PARA_N]]` markers. Source paragraph count: {source_paragraph_count}.
---
{chapter_text_with_markers}
---

Requirements:
  1. For each `[[PARA_N]]` marker in the source, your output MUST contain the
     same `[[PARA_N]]` marker on its own line, followed by the translated
     paragraph. Preserve every marker verbatim. Output exactly
     {source_paragraph_count} marker blocks for {source_paragraph_count} source
     blocks. Do NOT merge, split, drop, or invent markers.
  2. Output ONLY the marker-aligned translation. No preface ("Here is the
     translation:", "Translation:", "Sure, ..."), no commentary, no markdown
     headings, no code fences, no closing summary. The first characters of
     your output must be `[[PARA_1]]`.
  3. Translate every character / place / term from the glossary using the
     glossary's exact target form.
  4. Separate marker blocks with one blank line. Inside a single marker block,
     do not insert blank lines.
Traditional Chinese rule: If the target is 台灣繁體中文, output must use
繁體中文 (Traditional Chinese / Taiwan locale). 禁止使用任何簡體字。
常見錯誤對照：『学』→『學』、『为』→『為』、『这』→『這』、
『们』→『們』、『实』→『實』、『时』→『時』、『国』→『國』。

REGISTER-SPECIFIC RULES (matched to glossary.style_anchor.register):
{register_specific_rules}

  {custom_rule_number}. {custom_instructions}
  {verification_rule_number}. Before returning, count `[[PARA_` occurrences in
      your output. The count MUST equal {source_paragraph_count}. If not equal,
      add the missing markers + their translations or remove invented ones.
"""

DEFAULT_CUSTOM_INSTRUCTIONS = (
    "If the target is 台灣繁體中文, use 台灣用語 (e.g. 「軟體」not「软件」, "
    "「網路」not「网络」). Avoid 翻譯腔. Prefer short sentences over four-character "
    "literary clichés."
)

GENERIC_SUBAGENT_RULES = [
    "Plain target-language prose. Match the glossary's prefer/avoid lists in style_anchor.",
    "Glossary names are mandatory — use them verbatim throughout.",
    "Preserve paragraph boundaries and source paragraph count.",
    "Avoid 翻譯腔. Default to short, natural sentences over literary clichés.",
]

_LEAK_PREFIX_PATTERNS = [
    re.compile(r"^Here\s+(is|are)\s+(the|your)?\s*translation[^\n]*\n+", re.IGNORECASE),
    re.compile(r"^Translation\s*:\s*\n+", re.IGNORECASE),
    re.compile(r"^Sure[,!]?\s+[^\n]*\n+", re.IGNORECASE),
    re.compile(r"^Okay[,!]?\s+[^\n]*\n+", re.IGNORECASE),
    re.compile(r"^I'll\s+translate[^\n]*\n+", re.IGNORECASE),
]

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*\n(.*?)\n```\s*$", re.DOTALL)

# Model-emitted special tokens that leak past stop sequences. Hy-MT2 emits
# many shapes — the tokenizer recognises only the canonical
# `<｜hy_place▁holder▁no▁2｜>` and `<｜hy_end▁of▁sentence｜>`, so other
# end-of-turn intents come out as ordinary generated text and pick up
# random typos. Observed in the wild:
#   <｜hy-Assistant｜>, </｜hy-Assistant｜>     opening + closing forms
#   [｜hy-Assistant｜>                          bracket variant (`[` for `<`)
#   <｜hy-Assistantｯ>                          katakana ｯ (U+FF6F) typo
#   <｜hy_Assistant｜>, <｜hy_User｜>          underscore variants
#   </｜hy-Assient｜>, </｜hy-Assainer｜>      stem typos
#   <｜｜>                                     empty payload
# Plus mid-stream interruptions where the model started the token then
# jumped back to translation content without closing:
#   </｜hy-Ass麼？, <｜hy-Assistant時回饋
# Distinguisher from real HTML / list markup: a pipe-like char immediately
# after `<` or `[`, AND the body excludes whitespace / angle / brackets /
# pipes so `<br>`, `<p class>`, `</think>`, `[1,2,3]` are never eaten.
_MODEL_TOKEN_RE = re.compile(r"[<\[]\/?[｜|ｯ][^\s<>\[\]|｜ｯ]*[｜|ｯ]>")
# Truncated `hy-Assistant`-family prefixes (no proper closing token).
# Strips only the partial token; trailing real-content text is preserved.
# Body uses [A-Za-z0-9_] not \w — \w matches Chinese under Python's
# default UNICODE flag and would eat real translation content.
_PARTIAL_HY_TOKEN_RE = re.compile(r"[<\[]\/?[｜|ｯ]?hy[-_]?A[A-Za-z0-9_]*")


def contains_model_token_leak(text: str) -> bool:
    """Return whether generated text still contains a Hy-MT2 control token."""
    return bool(_MODEL_TOKEN_RE.search(text) or _PARTIAL_HY_TOKEN_RE.search(text))


_AUP_PHRASES = [
    "I cannot help with",
    "I'm unable to",
    "I won't be able",
    "I cannot reproduce",
    "I can't translate",
    "I'm not able to translate",
    "violate our usage policy",
    "copyrighted material",
    "copyright restrictions",
]

TRADITIONAL_CHINESE_ENFORCEMENT = (
    "輸出必須使用繁體中文（Traditional Chinese / Taiwan locale）。"
    "禁止使用任何簡體字。常見錯誤對照：『学』→『學』、『为』→『為』、"
    "『这』→『這』、『们』→『們』、『实』→『實』、『时』→『時』、"
    "『国』→『國』。"
)


def html_to_paragraphs(html: str) -> list[str]:
    """Convert chapter HTML to canonical plain-text paragraphs."""
    return extract_paragraphs(html)


def html_to_blocks(html: str) -> list[dict]:
    """Walk chapter HTML and return canonical ordered text/image blocks."""
    return extract_blocks(html)


def chapter_text_for_prompt(html: str) -> str:
    return "\n\n".join(html_to_paragraphs(html))


# Style rules for the offline path.
#
# NEVER add a sentence-LENGTH rule here. An earlier version opened with
# 「中譯單句超過 40 字就用句號斷成兩句」 and it wrecked the prose. Full-chapter
# measurements on Superagency ch.4 (106 paragraphs, 19 chunks, Qwopus3.6-27B-v2):
#
#   version                     >55char  mean  stdev   TTR  中譯（EN）  中國用語
#   old baseline (35B, no rules)  29.2%  44.7   27.4  .602      58        0
#   27B, no rules                 27.9%  44.0   27.3  .602      45       47
#   27B, length-rule version      10.0%  32.9   18.4  .605      11        3
#   27B, this version             31.2%  46.7   31.0  .604     104        2
#
# The length rule looked like a win on the >55-char metric (29% -> 10%) and was
# a large regression in readability: sentence-length stdev collapsed 27.4 -> 18.4
# (every sentence the same length reads flat) and inline source-term glosses were
# crushed 58 -> 11, because 「中譯（English）」 makes a sentence longer and the
# model dropped it to satisfy the length cap. The user rejected that output on
# reading it, while the >55-char metric said it was the best version. Optimising
# a length proxy optimises for monotony — target STRUCTURE (relative clauses,
# 複指) not length.
#
# The rules ARE needed on this model: without them the 27B emits 47 mainland-
# Chinese usages per chapter versus 0 for the 35B. Rules cut that to 2.
#
# Rules were originally stripped for hy-mt2:7b Q4_K_M (see
# build_ollama_chunk_prompt's docstring); that constraint expired when the
# default moved off 7B models on 2026-06-25.
#
# Known limits, do not overstate:
#  - Adequacy is NOT improved. Century mistranslations ("twenty-first century"
#    -> 二十世紀) occurred at 3/6 for BOTH the bare and the ruled prompt despite
#    an explicit 世紀/數字 clause. Explicit rules do not guarantee compliance.
#  - Style rules dilute the "keep English abbreviations verbatim" instruction
#    above them (AI/LLM/GPT verbatim survival 80% -> 53% on the length-rule
#    version; the model writes 人工智慧 instead). Content is not lost.
#  - The 中譯（English）rule fires per chunk, so a term is re-glossed in every
#    chunk that mentions it (104 glosses / 78 unique terms). Cross-chunk dedupe
#    is deterministic work — offline_postprocess.dedupe_inline_glosses handles
#    it. Do not try to fix that here; the model has no cross-chunk memory.
#  - Single book, single chapter for the v2 numbers. Treat as a local finding.
OFFLINE_STYLE_RULES = (
    "\n\n翻譯風格（不影響 marker 規則）：專有名詞、機構名、技術術語首次出現時"
    "寫成「中譯（English）」並列，之後只用中文。"
    "AI、LLM、GPT、RLHF、AGI、API 這類縮寫首次寫成「人工智慧（AI）」形式，"
    "之後直接用縮寫。"
    "英文關係子句改寫成獨立短句，不要用「，這些X……」複指硬接。"
    "台灣用語：網際網路、使用者、軟體、網路、資訊、品質、策略、反托拉斯；"
    "年代寫「1990 年代」，不寫「二十世紀九十年代」。"
    "長短句交錯，不要每句都短；拆句不可改動原意，"
    "世紀/數字/邏輯關係必須與原文一致。"
)

try:
    from scripts.term_matching import normalize_match_text, term_occurs_normalized
except ModuleNotFoundError:  # pragma: no cover - direct script execution path
    from term_matching import normalize_match_text, term_occurs_normalized

FIXED_TERMS_FILENAME = "spec_terms.json"


@functools.lru_cache(maxsize=8)
def load_fixed_terms(book_dir) -> dict[str, str]:
    """Per-book {source term: mandated translation} from <book_dir>/spec_terms.json.

    Spec §5.3 requires a per-book term table agreed before translation starts.
    Kept out of this repo — it is book-specific editorial data, not skill logic.
    Returns {} when the file is absent, so books without a table behave as before.
    Cached because the driver asks once per chunk (226 chunks on a 470K-char book).
    """
    from pathlib import Path

    path = Path(book_dir) / FIXED_TERMS_FILENAME
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    terms = data.get("terms", data) if isinstance(data, dict) else {}
    return {str(k): str(v) for k, v in terms.items() if k and v}


def select_terms_for_text(terms: dict[str, str], text: str) -> dict[str, str]:
    """Keep only the terms that actually occur in this chunk.

    Broadcasting the whole table into every chunk costs tokens and dilutes
    attention — the model reads past the instructions it needs. Measured on
    The Mind-Gut Connection (328 terms, 260 chunks): the full table is ~7000
    chars of prompt per chunk, while the terms a chunk actually uses average
    6.8 (median 6, max 20) — about 152 chars, a 97% reduction, and only 2 of
    260 chunks match nothing at all.

    Matching shares the builder's boundary, apostrophe, hyphen, diacritic and
    one-way ALL-CAPS rules. This prevents a key from passing build-time
    validation but being impossible to inject later.
    """
    if not terms or not text:
        return {}
    normalized_text = normalize_match_text(text)
    return {
        src: zh for src, zh in terms.items()
        if term_occurs_normalized(src, normalized_text)
    }


def format_fixed_terms(terms: dict[str, str]) -> str:
    """Render the term table as a compact prompt suffix. Empty string when unset."""
    if not terms:
        return ""
    pairs = "、".join(f"{k}＝{v}" for k, v in terms.items())
    return f"固定譯法，全書一致：{pairs}。"

OLLAMA_SYSTEM_PROMPT = (
    "You are a professional book translator. Translate every `[[PARA_N]]` "
    "block from English to {target_lang_long}. Echo each marker on its own "
    "line followed by the translated paragraph. Output exactly the same number "
    "of marker blocks as the input. Begin with `[[PARA_1]]`. No preface, no "
    "commentary, no markdown fences, no extra markers, no duplicate markers. "
    "Use 台灣繁體中文 (zh-Hant, Taiwan vocabulary). "
    f"{TRADITIONAL_CHINESE_ENFORCEMENT} "
    "Keep English abbreviations (AI / LLM / GPT / RLHF / AGI / API) verbatim — "
    "do not translate them into Chinese."
    f"{OFFLINE_STYLE_RULES}"
)


def build_ollama_chunk_prompt(
    *,
    chunk_paragraphs: list[str] | tuple[str, ...],
    target_lang: str = "zh-tw",
    carryover: str = "",
    fixed_terms: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Compact (system, user) prompt pair for local Ollama models.

    Strips the legal-context block, glossary JSON, style anchor, register
    rules, and self-verification rule from `SUBAGENT_PROMPT_TEMPLATE`. Those
    were designed for Anthropic Opus's larger attention budget; small local
    models (hy-mt2:7b Q4_K_M) treated the structured ~1500-token prompt as
    noise and sometimes stopped at the prompt boundary with empty output
    (observed `done_reason=stop, eval_count=1` on item_004/item_005 of The
    Next Renaissance).

    The marker contract lives in the system message so it's stable across
    requests; the user message carries only the per-chunk paragraphs +
    optional carryover.

    `fixed_terms` is the per-book term table (spec §5.3), appended to the system
    message as lookup data rather than as another rule. Measured 2026-08-09 on
    Superagency ch.4: prompt grew 756 -> 1071 chars with 10 terms, and the run
    still had 0 retries and 0 dropped paragraphs, so the table does not repeat
    the rule-count degradation documented above OFFLINE_STYLE_RULES.
    """
    paragraphs = list(chunk_paragraphs)
    target_lang_long = _target_long(target_lang)
    system = OLLAMA_SYSTEM_PROMPT.format(target_lang_long=target_lang_long)
    # Only the terms this chunk actually contains — see select_terms_for_text.
    system += format_fixed_terms(
        select_terms_for_text(fixed_terms or {}, "\n".join(paragraphs))
    )
    parts: list[str] = []
    if carryover.strip():
        parts.append(
            f"[Context — last paragraph of the prior chunk; for narrative flow only, "
            f"do not translate or repeat]:\n{carryover.strip()[-200:]}\n"
        )
    parts.append(
        f"[Translate the following {len(paragraphs)} paragraph block(s). "
        f"Output {len(paragraphs)} marker block(s) starting with `[[PARA_1]]`.]\n"
    )
    parts.append(ma.wrap_paragraphs(paragraphs))
    user = "\n".join(parts)
    return system, user


MINIMAL_PARAGRAPH_SYSTEM_PROMPT = (
    "Translate the user's English paragraph to {target_lang_long}. "
    "Output ONLY the translation. No preface, no commentary, no markdown, "
    "no English echo, no quotes. Use 台灣繁體中文 (Taiwan vocabulary). "
    f"{TRADITIONAL_CHINESE_ENFORCEMENT} "
    "Keep English abbreviations (AI / LLM / GPT) verbatim."
)


def build_minimal_paragraph_prompt(
    *,
    paragraph: str,
    target_lang: str = "zh-tw",
) -> tuple[str, str]:
    """Smallest possible prompt for a single paragraph — fallback floor.

    Used by translate_book_ollama.py when a chunk recursion reaches a single
    paragraph that still fails marker-aligned translation. No markers; the
    response IS the translation. Accept empty response as terminal (caller
    preserves source with note).
    """
    target_lang_long = _target_long(target_lang)
    system = MINIMAL_PARAGRAPH_SYSTEM_PROMPT.format(target_lang_long=target_lang_long)
    return system, paragraph.strip()


def build_subagent_prompt(
    *,
    chapter_label: str,
    book_title: str,
    target_lang: str,
    glossary: dict,
    style_sample: str,
    carryover: str,
    chapter_html: str,
    custom_instructions: str | None = None,
    register_override: str | None = None,
) -> str:
    target_lang_long = _target_long(target_lang)
    source_paragraphs = html_to_paragraphs(chapter_html)
    chapter_text_with_markers = ma.wrap_paragraphs(source_paragraphs)
    register_rules = resolve_register_rules(
        glossary,
        register_override=register_override,
        fallback_rules=GENERIC_SUBAGENT_RULES,
    )
    register_specific_rules = _format_register_specific_rules(register_rules, start=5)
    custom_rule_number = 5 + len(register_rules)
    verification_rule_number = custom_rule_number + 1
    return SUBAGENT_PROMPT_TEMPLATE.format(
        chapter_label=chapter_label,
        book_title=book_title,
        target_lang=target_lang,
        target_lang_long=target_lang_long,
        glossary_json=json.dumps(glossary, ensure_ascii=False, indent=2),
        style_sample=style_sample or "(no style sample yet — chapter 1)",
        carryover=carryover or "(this is the first chapter — no carryover)",
        chapter_text_with_markers=chapter_text_with_markers,
        source_paragraph_count=len(source_paragraphs),
        register_specific_rules=register_specific_rules,
        custom_rule_number=custom_rule_number,
        custom_instructions=custom_instructions or DEFAULT_CUSTOM_INSTRUCTIONS,
        verification_rule_number=verification_rule_number,
    )


def _format_register_specific_rules(rules: list[str], *, start: int) -> str:
    return "\n".join(f"  {index}. {rule}" for index, rule in enumerate(rules, start=start))


def sanitize_model_tokens(text: str) -> str:
    """Strip model-emitted special tokens that leaked past stop sequences.

    Two-pass: well-formed tokens first (any payload), then truncated
    `hy-Assistant`-family prefixes that broke mid-stream. Idempotent —
    safe to call on clean text.
    """
    text = _MODEL_TOKEN_RE.sub("", text or "")
    text = _PARTIAL_HY_TOKEN_RE.sub("", text)
    return text


def strip_known_leak_prefixes(raw: str) -> str:
    """Remove known leak prefixes, markdown fences, and model special-tokens
    from a subagent response.

    Idempotent — if the response is already clean, returns it unchanged.
    """
    # Special-token sweep first: catches prefix, suffix, AND inline leaks
    # in one pass (`<｜hy-Assistant｜>` typically lands mid-output, not as
    # a prefix). Doing this before the prefix matching keeps prefix regex
    # logic untouched.
    text = sanitize_model_tokens(raw or "").lstrip()
    # Markdown fence — pull body out
    fence_match = _FENCE_RE.match(text)
    if fence_match:
        text = fence_match.group(1).strip()
    # Preface patterns — strip recursively in case multiple stacked
    for _ in range(3):
        stripped = False
        for pat in _LEAK_PREFIX_PATTERNS:
            new_text = pat.sub("", text, count=1)
            if new_text != text:
                text = new_text.lstrip()
                stripped = True
        if not stripped:
            break
    return text


def detect_aup_refusal(raw: str) -> str | None:
    """If the response looks like an AUP refusal, return a short reason string;
    otherwise return None. Checks the first 800 chars to avoid false-positives
    from in-body discussions of copyright."""
    if not raw:
        return None
    head = raw[:800].lower()
    for phrase in _AUP_PHRASES:
        if phrase.lower() in head:
            return f"AUP refusal phrase detected: {phrase!r}"
    return None


def validate_translation(translation: str, chapter_html: str, *, min_ratio: float = 0.5) -> list[str]:
    """Sanity checks on a subagent return. Returns warning list (empty = OK).

    Checks:
      - non-empty
      - if response contains any `[[PARA_` markers, verify marker alignment
        against source paragraph count (missing / extra markers reported)
      - otherwise fall back to paragraph-count ratio (legacy behavior)
      - no obvious refusal patterns ("I cannot", "I'm sorry", "As an AI")
      - no obvious preface leak ("Here is the translation", "Translation:")
    """
    warnings: list[str] = []
    translation = (translation or "").strip()
    if not translation:
        warnings.append("translation is empty")
        return warnings

    # AUP refusal short-circuits: signal it loudly so caller marks aup_refused
    aup_reason = detect_aup_refusal(translation)
    if aup_reason:
        warnings.append(f"AUP_REFUSED: {aup_reason}")
        return warnings  # do not also report marker/ratio issues

    src_paras = html_to_paragraphs(chapter_html)

    # Marker path (preferred)
    if "[[PARA_" in translation.upper():
        result = ma.parse_marker_output(translation, expected_count=len(src_paras))
        if result.missing_markers:
            preview = ", ".join(f"PARA_{n}" for n in result.missing_markers[:5])
            more = "" if len(result.missing_markers) <= 5 else f" (+{len(result.missing_markers)-5} more)"
            warnings.append(f"missing markers: {preview}{more}")
        if result.duplicate_markers:
            preview = ", ".join(f"PARA_{n}" for n in result.duplicate_markers[:5])
            more = "" if len(result.duplicate_markers) <= 5 else f" (+{len(result.duplicate_markers)-5} more)"
            warnings.append(f"duplicate markers: {preview}{more}")
        if result.extra_markers:
            preview = ", ".join(f"PARA_{n}" for n in result.extra_markers[:5])
            more = "" if len(result.extra_markers) <= 5 else f" (+{len(result.extra_markers)-5} more)"
            warnings.append(f"extra markers (not in source): {preview}{more}")
    else:
        # Legacy fallback: paragraph-count ratio
        tgt_paras = [p for p in translation.split("\n\n") if p.strip()]
        if src_paras and len(tgt_paras) < max(1, int(len(src_paras) * min_ratio)):
            warnings.append(
                f"translation has {len(tgt_paras)} paragraphs vs source {len(src_paras)} "
                f"(below {min_ratio:.0%} ratio; expected marker-aligned output)"
            )

    refusal_re = re.compile(r"\b(I (cannot|can't|won't|am unable)|I'm sorry|As an AI)\b", re.IGNORECASE)
    if refusal_re.search(translation[:500]):
        warnings.append("translation contains apparent refusal language")

    preface_re = re.compile(r"^(Here\s+(is|are)|Translation\s*:|Sure[,!]|Okay[,!])", re.IGNORECASE)
    if preface_re.search(translation[:80]):
        warnings.append("translation starts with a preface phrase (should start with [[PARA_1]])")

    return warnings


def extract_aligned_translation(raw_output: str, expected_count: int) -> str:
    """Parse a marker-aligned subagent response back to plain translation text.

    Joins translations of [[PARA_1]]..[[PARA_N]] with one blank line, stripped
    of marker tokens themselves. Raises ValueError on misalignment so callers
    can catch and route to the retry / AUP / manual-fix path.
    """
    cleaned = strip_known_leak_prefixes(raw_output)
    result = ma.parse_marker_output(cleaned, expected_count=expected_count)
    if result.missing_markers:
        raise ValueError(f"missing markers in subagent output: {result.missing_markers}")
    if result.duplicate_markers:
        raise ValueError(f"duplicate markers in subagent output: {result.duplicate_markers}")
    if result.extra_markers:
        raise ValueError(f"extra markers in subagent output: {result.extra_markers}")
    return "\n\n".join(result.translations)


def _target_long(target_lang: str) -> str:
    table = {
        "zh-tw": "台灣繁體中文",
        "zh-cn": "簡體中文（中國大陸）",
        "en": "English",
        "ja": "日本語",
        "ko": "한국어",
    }
    return table.get(target_lang.lower(), target_lang)

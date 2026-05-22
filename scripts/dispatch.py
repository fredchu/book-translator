"""Subagent dispatch prompt + helpers.

The Agent tool call happens in the main Claude Code session. This module gives
the main session:
  - build_subagent_prompt(): assemble the full per-chapter translation prompt
  - extract_translation_text(): clean a chapter HTML down to plain text for input
  - validate_translation(): cheap structural checks on returned translation
"""

from __future__ import annotations

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


def html_to_paragraphs(html: str) -> list[str]:
    """Convert chapter HTML to canonical plain-text paragraphs."""
    return extract_paragraphs(html)


def html_to_blocks(html: str) -> list[dict]:
    """Walk chapter HTML and return canonical ordered text/image blocks."""
    return extract_blocks(html)


def chapter_text_for_prompt(html: str) -> str:
    return "\n\n".join(html_to_paragraphs(html))


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

    src_paras = html_to_paragraphs(chapter_html)

    # Marker path (preferred)
    if "[[PARA_" in translation.upper():
        result = ma.parse_marker_output(translation, expected_count=len(src_paras))
        if result.missing_markers:
            preview = ", ".join(f"PARA_{n}" for n in result.missing_markers[:5])
            more = "" if len(result.missing_markers) <= 5 else f" (+{len(result.missing_markers)-5} more)"
            warnings.append(f"missing markers: {preview}{more}")
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


def _target_long(target_lang: str) -> str:
    table = {
        "zh-tw": "台灣繁體中文",
        "zh-cn": "簡體中文（中國大陸）",
        "en": "English",
        "ja": "日本語",
        "ko": "한국어",
    }
    return table.get(target_lang.lower(), target_lang)

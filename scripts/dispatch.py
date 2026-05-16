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
except ImportError:  # pragma: no cover
    from content_blocks import extract_blocks, extract_paragraphs
    from glossary import resolve_register_rules

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

CHAPTER {chapter_label} SOURCE TEXT (source paragraph count: {source_paragraph_count};
paragraph boundaries preserved with blank lines between paragraphs):
---
{chapter_text}
---

Requirements:
  1. Output ONLY the translated text. No commentary, no markdown headings, no
     "Translation:" prefix.
  2. Separate paragraphs with EXACTLY one blank line — that is, two consecutive newline
     characters (`\\n\\n`) between paragraphs. Within a single paragraph, do not insert blank
     lines. Single newlines (`\\n`) without a blank line do NOT separate paragraphs in this
     format. This rule applies even when the chapter has only 2-3 paragraphs.
  3. Translate every character / place / term from the glossary using the
     glossary's exact target form.
  4. Do not omit any paragraph. Translate every paragraph in order.

REGISTER-SPECIFIC RULES (matched to glossary.style_anchor.register):
{register_specific_rules}

  {custom_rule_number}. {custom_instructions}
  {verification_rule_number}. Before returning, split your output by the exact string `\\n\\n` and count
      non-empty paragraphs. This count MUST equal the source paragraph count
      above. If not equal, fix the separator (most common cause: used `\\n`
      instead of `\\n\\n` between short paragraphs).
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
    chapter_text = "\n\n".join(source_paragraphs)
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
        chapter_text=chapter_text,
        source_paragraph_count=len(source_paragraphs),
        register_specific_rules=register_specific_rules,
        custom_rule_number=custom_rule_number,
        custom_instructions=custom_instructions or DEFAULT_CUSTOM_INSTRUCTIONS,
        verification_rule_number=verification_rule_number,
    )


def _format_register_specific_rules(rules: list[str], *, start: int) -> str:
    return "\n".join(f"  {index}. {rule}" for index, rule in enumerate(rules, start=start))


def validate_translation(translation: str, chapter_html: str, *, min_ratio: float = 0.5) -> list[str]:
    """Cheap sanity checks. Returns a list of human-readable warnings; empty list = OK.

    Checks:
      - non-empty
      - paragraph count >= min_ratio * source paragraph count
      - no obvious refusal patterns ("I cannot", "I'm sorry", "As an AI")
    """
    warnings: list[str] = []
    translation = (translation or "").strip()
    if not translation:
        warnings.append("translation is empty")
        return warnings
    src_paras = html_to_paragraphs(chapter_html)
    tgt_paras = [p for p in translation.split("\n\n") if p.strip()]
    if src_paras and len(tgt_paras) < max(1, int(len(src_paras) * min_ratio)):
        warnings.append(
            f"translation has {len(tgt_paras)} paragraphs vs source {len(src_paras)} "
            f"(below {min_ratio:.0%} ratio)"
        )
    refusal_re = re.compile(r"\b(I (cannot|can't|won't|am unable)|I'm sorry|As an AI)\b", re.IGNORECASE)
    if refusal_re.search(translation[:500]):
        warnings.append("translation contains apparent refusal language")
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

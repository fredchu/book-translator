# /book-translator Stability Upgrades Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Borrow the four design choices from Bocky's `bilingual-book-translator` that make his pipeline 「穩定又通順」, and graft them onto `/book-translator` without changing its core architecture (per-chapter subagent dispatch in parallel batches). Result: kills the漏譯 / 格式錯誤 / 30% copyright refuse symptoms while keeping parallel speed.

**Architecture:** No new top-level architecture. Five surgical changes:
1. `[[PARA_N]]` markers inside the subagent prompt force structural alignment between source paragraphs and translated output.
2. AUP refuse becomes a recognized terminal state per chapter — source preserved + bilingual annotation, not a hard failure.
3. Subagent prompt gets tighter purity rules + post-validation strips known leak patterns ("Here is the translation:", markdown fences, persona markers).
4. Subagent default model becomes Sonnet 4.6 (Opus 4.7 reserved for ch.01 main-session style anchor + escalation on validation reject).
5. Per-chapter prompt + raw subagent response + parsed output land in `<book_dir>/translation_log/ch_NNN.json` so a bad chapter can be re-processed offline without re-extracting the EPUB.

**Tech Stack:** Python 3, existing `~/.claude/skills/book-translator/` modules, pytest. No new runtime dependencies.

---

## Scope

**In:** All edits to `~/.claude/skills/book-translator/` — `dispatch.py`, `state.py`, `assemble.py`, audit modules, `SKILL.md`, `CHANGELOG.md`, tests.

**Out:**
- Any change to Bocky's `~/.claude/skills/bilingual-book-translator/`.
- Any change to `inbox/translations/Bocky_books_20260519/` (translation orchestration / book runs are user-driven via terminal — not in this plan).
- Cross-modal eval gate changes (Phase 3 stuff in SKILL.md is separate machinery; we don't touch it).
- Cross-book shared glossary, Discord notifications, launchd schedulers.
- A wholesale rewrite to per-chunk Agent dispatch (the within-chapter chunk loop). Marker alignment gives us 80% of the benefit at 20% of the work; the chunk-dispatch rewrite is explicitly deferred to a future round.

## File Structure

**Existing files modified:**
- `~/.claude/skills/book-translator/scripts/dispatch.py` — prompt builder + marker wrap/parse + validators
- `~/.claude/skills/book-translator/scripts/state.py` — add `AUP_REFUSED` status + `mark_aup_refused()`
- `~/.claude/skills/book-translator/scripts/assemble.py` — handle `aup_refused` chapters
- `~/.claude/skills/book-translator/scripts/translation_quality_audit.py` — accept `[[AUP_REFUSED]]` marker as valid
- `~/.claude/skills/book-translator/scripts/bilingual_coverage_audit.py` — same
- `~/.claude/skills/book-translator/SKILL.md` — document new behaviors
- `~/.claude/skills/book-translator/CHANGELOG.md` — Unreleased section
- Existing tests for the above modules — extend, don't break

**New files:**
- `~/.claude/skills/book-translator/scripts/marker_alignment.py` — wrap source paragraphs with `[[PARA_N]]` markers, parse aligned output back to per-paragraph translations
- `~/.claude/skills/book-translator/scripts/translation_log.py` — write/read `<book_dir>/translation_log/ch_NNN.json`
- `~/.claude/skills/book-translator/scripts/replay_chapter.py` — CLI: given a logged chapter, re-validate + re-emit translation text from raw response
- `~/.claude/skills/book-translator/test/test_marker_alignment.py`
- `~/.claude/skills/book-translator/test/test_translation_log.py`
- `~/.claude/skills/book-translator/test/test_aup_refused.py` — coverage for state.py + assemble.py interactions
- `~/.claude/skills/book-translator/docs/plans/2026-05-19-stability-upgrades.md` — this file

**Runtime artifact (user's book dirs, not in repo):**
- `<book_dir>/translation_log/ch_NNN.json` — written per chapter at subagent return

---

## Phase 1: Marker Alignment

### Task 1: Build `marker_alignment.py`

**Files:**
- Create: `~/.claude/skills/book-translator/scripts/marker_alignment.py`
- Create: `~/.claude/skills/book-translator/test/test_marker_alignment.py`

- [ ] **Step 1: Write the failing test**

Write `test/test_marker_alignment.py`:

```python
"""Unit tests for marker_alignment — wrap source + parse translated output."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import marker_alignment as ma  # type: ignore  # noqa: E402


def test_wrap_paragraphs_inserts_sequential_markers():
    paras = ["First para.", "Second para.", "Third para."]
    text = ma.wrap_paragraphs(paras)
    assert "[[PARA_1]]" in text
    assert "[[PARA_2]]" in text
    assert "[[PARA_3]]" in text
    assert text.index("[[PARA_1]]") < text.index("[[PARA_2]]") < text.index("[[PARA_3]]")
    # marker must precede the paragraph it labels
    assert text.index("[[PARA_2]]") < text.index("Second para.")


def test_wrap_paragraphs_handles_single_paragraph():
    paras = ["Only one."]
    text = ma.wrap_paragraphs(paras)
    assert "[[PARA_1]]" in text
    assert "Only one." in text


def test_wrap_paragraphs_empty_input_returns_empty_string():
    assert ma.wrap_paragraphs([]) == ""


def test_parse_marker_output_extracts_translations_in_order():
    output = "[[PARA_1]]\n第一段。\n\n[[PARA_2]]\n第二段。\n\n[[PARA_3]]\n第三段。"
    result = ma.parse_marker_output(output, expected_count=3)
    assert result.translations == ["第一段。", "第二段。", "第三段。"]
    assert result.missing_markers == []
    assert result.extra_markers == []
    assert result.is_aligned is True


def test_parse_marker_output_detects_missing_marker():
    # Subagent dropped PARA_2
    output = "[[PARA_1]]\n第一段。\n\n[[PARA_3]]\n第三段。"
    result = ma.parse_marker_output(output, expected_count=3)
    assert result.is_aligned is False
    assert result.missing_markers == [2]
    # parsed translations for the markers that DID appear
    assert result.translations_by_idx == {1: "第一段。", 3: "第三段。"}


def test_parse_marker_output_detects_extra_marker():
    # Subagent invented PARA_4
    output = "[[PARA_1]]\n第一段。\n\n[[PARA_2]]\n第二段。\n\n[[PARA_4]]\n第四段?"
    result = ma.parse_marker_output(output, expected_count=2)
    assert result.is_aligned is False
    assert result.extra_markers == [4]


def test_parse_marker_output_tolerates_whitespace_variants():
    output = "[[ PARA_1 ]]\n第一段\n\n[[para_2]]\n第二段"  # space + lowercase
    result = ma.parse_marker_output(output, expected_count=2)
    # we accept tolerant matching
    assert result.is_aligned is True
    assert result.translations == ["第一段", "第二段"]


def test_parse_marker_output_no_markers_at_all():
    output = "整段都沒有 marker"
    result = ma.parse_marker_output(output, expected_count=3)
    assert result.is_aligned is False
    assert result.missing_markers == [1, 2, 3]
    assert result.translations == []
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /Users/fredchu/.claude/skills/book-translator
python3 -m pytest test/test_marker_alignment.py -v
```

Expected: `ModuleNotFoundError: No module named 'marker_alignment'`.

- [ ] **Step 3: Write the module**

Write `scripts/marker_alignment.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/fredchu/.claude/skills/book-translator
python3 -m pytest test/test_marker_alignment.py -v
```

Expected: all 8 tests pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/fredchu/.claude/skills/book-translator
git add scripts/marker_alignment.py test/test_marker_alignment.py
git commit -m "$(cat <<'EOF'
feat(marker-alignment): [[PARA_N]] marker wrap + parse module

Source paragraphs get numbered markers in the subagent prompt; output is parsed
by regex matching the markers. Tolerant of whitespace/case variants. Result
exposes missing_markers + extra_markers for downstream validation.

Borrowed from Bocky's bilingual-book-translator design: marker contract gives
structural verification instead of fragile blank-line paragraph counting.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Task 2: Wire markers into `dispatch.build_subagent_prompt`

**Files:**
- Modify: `~/.claude/skills/book-translator/scripts/dispatch.py`
- Modify: `~/.claude/skills/book-translator/test/test_dispatch.py`

- [ ] **Step 1: Write the failing test**

Append to `test/test_dispatch.py`:

```python
def test_build_subagent_prompt_uses_para_markers():
    """Subagent prompt should wrap source paragraphs with [[PARA_N]] markers."""
    glossary = {
        "characters": {}, "places": {}, "terms": {},
        "style_anchor": {"register": "x", "avoid": [], "prefer": []},
    }
    prompt = dispatch.build_subagent_prompt(
        chapter_label="2", book_title="X", target_lang="zh-tw",
        glossary=glossary, style_sample="", carryover="",
        chapter_html="<p>Alpha.</p><p>Beta.</p><p>Gamma.</p>",
    )
    # Source side wrapped with markers
    assert "[[PARA_1]]" in prompt
    assert "[[PARA_2]]" in prompt
    assert "[[PARA_3]]" in prompt
    # marker contract in the requirements
    assert "[[PARA_N]]" in prompt or "[[PARA_" in prompt
    assert "Preserve every [[PARA_" in prompt or "preserve every marker" in prompt.lower()


def test_build_subagent_prompt_marker_count_matches_source():
    """Marker count == source paragraph count."""
    glossary = {
        "characters": {}, "places": {}, "terms": {},
        "style_anchor": {"register": "x", "avoid": [], "prefer": []},
    }
    html = "<p>A.</p><p>B.</p><p>C.</p><p>D.</p><p>E.</p>"
    prompt = dispatch.build_subagent_prompt(
        chapter_label="3", book_title="Y", target_lang="zh-tw",
        glossary=glossary, style_sample="", carryover="",
        chapter_html=html,
    )
    import re
    markers = re.findall(r"\[\[PARA_(\d+)\]\]", prompt)
    # source side should have 5 markers (PARA_1..PARA_5)
    # Note: prompt may mention [[PARA_N]] generically in rules, count distinct numbers
    numeric = [int(m) for m in markers if m.isdigit()]
    assert max(numeric) == 5
    assert set(numeric) >= {1, 2, 3, 4, 5}
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /Users/fredchu/.claude/skills/book-translator
python3 -m pytest test/test_dispatch.py::test_build_subagent_prompt_uses_para_markers test/test_dispatch.py::test_build_subagent_prompt_marker_count_matches_source -v
```

Expected: both fail (current prompt joins with `\n\n` without markers).

- [ ] **Step 3: Modify `dispatch.py`**

In `scripts/dispatch.py`:

Replace the import block near the top to add `marker_alignment`:

```python
try:  # pragma: no cover - import mode depends on caller
    from .content_blocks import extract_blocks, extract_paragraphs
    from .glossary import resolve_register_rules
    from . import marker_alignment as ma
except ImportError:  # pragma: no cover
    from content_blocks import extract_blocks, extract_paragraphs
    from glossary import resolve_register_rules
    import marker_alignment as ma  # type: ignore
```

Replace `SUBAGENT_PROMPT_TEMPLATE` with this version (note: paragraph separator rule replaced by marker rule):

```python
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
```

Replace `build_subagent_prompt` body — replace `chapter_text = "\n\n".join(source_paragraphs)` with marker wrap, rename the format parameter, and pass `chapter_text_with_markers`:

```python
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
```

Also update the older paragraph-separator test guards in `test_dispatch.py` that are now outdated. Replace this section in `test/test_dispatch.py`:

```python
def test_subagent_prompt_template_makes_paragraph_separator_explicit():
    assert "EXACTLY one blank line" in dispatch.SUBAGENT_PROMPT_TEMPLATE
    assert "two consecutive newline" in dispatch.SUBAGENT_PROMPT_TEMPLATE
    assert "Single newlines (`\\n`) without a blank line do NOT separate paragraphs" in (
        dispatch.SUBAGENT_PROMPT_TEMPLATE
    )
    assert "split your output by the exact string `\\n\\n`" in dispatch.SUBAGENT_PROMPT_TEMPLATE
```

with the new marker-based assertion:

```python
def test_subagent_prompt_template_makes_marker_contract_explicit():
    assert "[[PARA_N]]" in dispatch.SUBAGENT_PROMPT_TEMPLATE
    assert "marker" in dispatch.SUBAGENT_PROMPT_TEMPLATE.lower()
    assert "Preserve every marker verbatim" in dispatch.SUBAGENT_PROMPT_TEMPLATE
    # the first characters of output must be [[PARA_1]]
    assert "[[PARA_1]]" in dispatch.SUBAGENT_PROMPT_TEMPLATE
```

- [ ] **Step 4: Run all dispatch tests**

```bash
cd /Users/fredchu/.claude/skills/book-translator
python3 -m pytest test/test_dispatch.py -v
```

Expected: all pass (including the 2 new marker tests and the renamed template test).

- [ ] **Step 5: Commit**

```bash
cd /Users/fredchu/.claude/skills/book-translator
git add scripts/dispatch.py test/test_dispatch.py
git commit -m "$(cat <<'EOF'
feat(dispatch): wrap source paragraphs with [[PARA_N]] markers in subagent prompt

Replace the blank-line paragraph contract with marker-based structural alignment.
Source side gets [[PARA_1]]..[[PARA_N]]; subagent must echo each marker followed
by its translation. Verification rule asks the subagent to count markers
before returning.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Task 3: Add `validate_translation` marker-based check

**Files:**
- Modify: `~/.claude/skills/book-translator/scripts/dispatch.py`
- Modify: `~/.claude/skills/book-translator/test/test_dispatch.py`

- [ ] **Step 1: Write the failing test**

Append to `test/test_dispatch.py`:

```python
def test_validate_translation_detects_missing_markers():
    src_html = "<p>A.</p><p>B.</p><p>C.</p>"
    # subagent dropped PARA_2
    translation = "[[PARA_1]]\n甲\n\n[[PARA_3]]\n丙"
    warnings = dispatch.validate_translation(translation, src_html)
    assert any("missing markers" in w.lower() or "PARA_2" in w for w in warnings)


def test_validate_translation_passes_aligned_marker_output():
    src_html = "<p>A.</p><p>B.</p>"
    translation = "[[PARA_1]]\n甲\n\n[[PARA_2]]\n乙"
    warnings = dispatch.validate_translation(translation, src_html)
    assert warnings == []


def test_validate_translation_detects_invented_marker():
    src_html = "<p>A.</p><p>B.</p>"
    # subagent added PARA_3
    translation = "[[PARA_1]]\n甲\n\n[[PARA_2]]\n乙\n\n[[PARA_3]]\n丙?"
    warnings = dispatch.validate_translation(translation, src_html)
    assert any("extra" in w.lower() or "PARA_3" in w for w in warnings)
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /Users/fredchu/.claude/skills/book-translator
python3 -m pytest test/test_dispatch.py::test_validate_translation_detects_missing_markers test/test_dispatch.py::test_validate_translation_passes_aligned_marker_output test/test_dispatch.py::test_validate_translation_detects_invented_marker -v
```

Expected: aligned-output test may pass (warnings empty), but missing/extra detection tests fail (current validator only checks paragraph ratio, doesn't know about markers).

- [ ] **Step 3: Modify `dispatch.py`**

Replace `validate_translation` with this marker-aware version:

```python
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
```

- [ ] **Step 4: Run all dispatch tests**

```bash
cd /Users/fredchu/.claude/skills/book-translator
python3 -m pytest test/test_dispatch.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/fredchu/.claude/skills/book-translator
git add scripts/dispatch.py test/test_dispatch.py
git commit -m "$(cat <<'EOF'
feat(dispatch): marker-aware validate_translation + preface leak detection

When the response contains [[PARA_ markers, validate by marker alignment
(missing / extra). Otherwise fall back to legacy paragraph-ratio check.
Also flag obvious preface phrases like 'Here is the translation:' which
indicate the subagent ignored the 'output must start with [[PARA_1]]' rule.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Task 4: Add `extract_aligned_translation` helper

**Files:**
- Modify: `~/.claude/skills/book-translator/scripts/dispatch.py`
- Modify: `~/.claude/skills/book-translator/test/test_dispatch.py`

- [ ] **Step 1: Write the failing test**

Append to `test/test_dispatch.py`:

```python
def test_extract_aligned_translation_joins_paragraphs_with_blank_line():
    output = "[[PARA_1]]\n第一段。\n\n[[PARA_2]]\n第二段。"
    text = dispatch.extract_aligned_translation(output, expected_count=2)
    assert text == "第一段。\n\n第二段。"


def test_extract_aligned_translation_raises_when_misaligned():
    output = "[[PARA_1]]\n第一段。"  # missing PARA_2
    with pytest.raises(ValueError) as exc:
        dispatch.extract_aligned_translation(output, expected_count=2)
    assert "missing" in str(exc.value).lower() or "PARA_2" in str(exc.value)


def test_extract_aligned_translation_strips_marker_lines_from_body():
    output = "[[PARA_1]]\n甲\n\n[[PARA_2]]\n乙"
    text = dispatch.extract_aligned_translation(output, expected_count=2)
    # the marker tokens themselves must not appear in the joined output
    assert "[[PARA_1]]" not in text
    assert "[[PARA_2]]" not in text
```

(`pytest` is already imported at the top of `test_dispatch.py`? Check — if not, add `import pytest` at the top of the test file.)

- [ ] **Step 2: Run test to verify failure**

```bash
cd /Users/fredchu/.claude/skills/book-translator
python3 -m pytest test/test_dispatch.py::test_extract_aligned_translation_joins_paragraphs_with_blank_line -v
```

Expected: `AttributeError: module 'dispatch' has no attribute 'extract_aligned_translation'`.

- [ ] **Step 3: Modify `dispatch.py`**

Add the new function near the bottom of `scripts/dispatch.py`:

```python
def extract_aligned_translation(raw_output: str, expected_count: int) -> str:
    """Parse a marker-aligned subagent response back to plain translation text.

    Joins translations of [[PARA_1]]..[[PARA_N]] with one blank line, stripped
    of marker tokens themselves. Raises ValueError on misalignment so callers
    can catch and route to the retry / AUP / manual-fix path.
    """
    result = ma.parse_marker_output(raw_output, expected_count=expected_count)
    if result.missing_markers:
        raise ValueError(f"missing markers in subagent output: {result.missing_markers}")
    if result.extra_markers:
        raise ValueError(f"extra markers in subagent output: {result.extra_markers}")
    return "\n\n".join(result.translations)
```

Also ensure `import pytest` is at the top of `test/test_dispatch.py` if not already there.

- [ ] **Step 4: Run all dispatch tests**

```bash
cd /Users/fredchu/.claude/skills/book-translator
python3 -m pytest test/test_dispatch.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/fredchu/.claude/skills/book-translator
git add scripts/dispatch.py test/test_dispatch.py
git commit -m "$(cat <<'EOF'
feat(dispatch): extract_aligned_translation — strip markers, join with blank line

Convert marker-aligned subagent output into plain chapter text that downstream
assemble.py can splice into the bilingual EPUB. Raises ValueError on
misalignment so the main session can route to retry / AUP / manual-fix.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Task 5: Update SKILL.md Step 6 to reflect marker contract

**Files:**
- Modify: `~/.claude/skills/book-translator/SKILL.md`

- [ ] **Step 1: Read current Step 6 section**

```bash
grep -n "Step 6" /Users/fredchu/.claude/skills/book-translator/SKILL.md
```

Locate the section "Step 6: Parallel subagent dispatch (translate items only)" — read 30 lines around it.

- [ ] **Step 2: Replace the subagent prompt sample block**

Replace the literal example prompt in Step 6 (the indented block starting with `You are translating Chapter <N>...`) with the marker-aware example. The block should now reference `dispatch.build_subagent_prompt` (the python builder) as the source of truth, with a short prose summary:

```markdown
### Step 6: Parallel subagent dispatch (translate items only)

Use the `Agent` tool with `model: "sonnet"` for ch.02+ (see "Model selection
discipline" below), batch size = `concurrency` (default 5). Each task's
prompt is produced by `dispatch.build_subagent_prompt(...)` and includes:

- Glossary (mandatory translations)
- Style anchor (first 500 chars of ch.01 translation)
- Carryover (last 200 chars of ch.<N-1> translation)
- **Source paragraphs wrapped with `[[PARA_1]]`..`[[PARA_N]]` markers**
- Verification rules requiring the subagent to echo every marker followed by
  its translation, starting the response with `[[PARA_1]]` (no preface)

After each subagent returns, the main session:

1. Calls `dispatch.validate_translation(raw, chapter_html)` to surface warnings
   (missing/extra markers, refusal language, preface leak).
2. On a clean run: calls `dispatch.extract_aligned_translation(raw, N)` to get
   the plain translation text, then `state.mark_done(state, chapter_id, text)`.
3. On misalignment: retry the chapter once with the same prompt; if it still
   misaligns, escalate to Opus (see Phase 4) or mark `aup_refused` if the
   response contains refusal language; mark `failed` otherwise.
```

- [ ] **Step 3: Verify SKILL.md still loads as a valid skill (frontmatter intact)**

```bash
head -15 /Users/fredchu/.claude/skills/book-translator/SKILL.md
```

Expected: frontmatter starts with `---` and contains `name: book-translator`.

- [ ] **Step 4: Commit**

```bash
cd /Users/fredchu/.claude/skills/book-translator
git add SKILL.md
git commit -m "$(cat <<'EOF'
docs(skill): Step 6 reflects [[PARA_N]] marker contract

Subagent prompt now wraps source paragraphs with markers and validates
alignment via dispatch.validate_translation + dispatch.extract_aligned_translation
on return. Document the misalignment retry path.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase 2: AUP Refuse Fallback

### Task 6: Add `aup_refused` status to `state.py`

**Files:**
- Modify: `~/.claude/skills/book-translator/scripts/state.py`
- Modify: `~/.claude/skills/book-translator/test/test_state.py`

- [ ] **Step 1: Write the failing test**

Append to `test/test_state.py`:

```python
def test_mark_aup_refused_sets_status_and_preserves_strategy():
    state = {
        "book": "x.epub", "started": "2026-05-19T00:00:00Z", "target_lang": "zh-tw",
        "glossary_built": True, "style_confirmed": True,
        "chapters": {
            "item_005": {"output_strategy": "translate", "status": "in_progress"},
        },
    }
    state_module.mark_aup_refused(state, "item_005", "Anthropic AUP refused this section")
    entry = state["chapters"]["item_005"]
    assert entry["status"] == "aup_refused"
    assert entry["output_strategy"] == "translate"  # preserved, not flipped to drop
    assert "AUP" in entry["reason"]


def test_validate_state_accepts_aup_refused():
    state = {
        "book": "x.epub", "started": "2026-05-19T00:00:00Z", "target_lang": "zh-tw",
        "glossary_built": True, "style_confirmed": True,
        "chapters": {
            "item_001": {"output_strategy": "translate", "status": "aup_refused",
                         "reason": "refused"},
        },
    }
    # should NOT raise
    state_module.validate_state(state, require_strategy=False)


def test_chapters_by_status_returns_aup_refused():
    state = {
        "book": "x.epub", "started": "2026-05-19T00:00:00Z", "target_lang": "zh-tw",
        "glossary_built": True, "style_confirmed": True,
        "chapters": {
            "item_001": {"output_strategy": "translate", "status": "aup_refused",
                         "reason": "x"},
            "item_002": {"output_strategy": "translate", "status": "done"},
        },
    }
    assert state_module.chapters_by_status(state, "aup_refused") == ["item_001"]
```

Make sure `state_module` is imported at the top of the test file (existing convention).

- [ ] **Step 2: Run test to verify failure**

```bash
cd /Users/fredchu/.claude/skills/book-translator
python3 -m pytest test/test_state.py -k aup -v
```

Expected: `AttributeError: module 'state' has no attribute 'mark_aup_refused'` + the validate test fails because `aup_refused` is not in `VALID_STATUSES`.

- [ ] **Step 3: Modify `scripts/state.py`**

Near the top with the other status constants:

```python
AUP_REFUSED = "aup_refused"
```

Add `AUP_REFUSED` to `VALID_STATUSES`:

```python
VALID_STATUSES = {PENDING, IN_PROGRESS, DONE, FAILED, SOURCE_READY, DROPPED, AUP_REFUSED}
```

Add a `mark_aup_refused` method to `ChapterEntry`:

```python
    def mark_aup_refused(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("aup_refused requires a non-empty reason")
        self.status = AUP_REFUSED
        self.retry_count = 0
        self.error = None
        self.translation_hash = None
        self.carryover = None
        self.reason = reason
```

Add a module-level helper:

```python
def mark_aup_refused(state: dict, chapter_id: str, reason: str) -> None:
    entry = ChapterEntry.from_dict(state["chapters"].get(chapter_id, {}))
    if entry.output_strategy == "":
        entry.output_strategy = TRANSLATE
    entry.mark_aup_refused(reason)
    state["chapters"][chapter_id] = entry.to_dict()
```

Update the docstring at top of file to mention `aup_refused` in the Status values list.

- [ ] **Step 4: Run all state tests**

```bash
cd /Users/fredchu/.claude/skills/book-translator
python3 -m pytest test/test_state.py -v
```

Expected: all pass including the 3 new aup tests.

- [ ] **Step 5: Commit**

```bash
cd /Users/fredchu/.claude/skills/book-translator
git add scripts/state.py test/test_state.py
git commit -m "$(cat <<'EOF'
feat(state): add aup_refused status + mark_aup_refused helper

When the subagent's response trips Anthropic AUP refusal, the chapter is not
'failed' (which implies retryable) but 'aup_refused' (terminal — source will
be preserved with bilingual annotation in assembly). output_strategy stays
'translate' so the structural audit still sees the spine entry as expected.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Task 7: Handle `aup_refused` chapters in `assemble.py`

**Files:**
- Modify: `~/.claude/skills/book-translator/scripts/assemble.py` (and probably `scripts/bilingual_rewriter.py`)
- Create: `~/.claude/skills/book-translator/test/test_aup_refused.py`

- [ ] **Step 1: Write the failing test**

Write `test/test_aup_refused.py`:

```python
"""End-to-end test: aup_refused chapter preserves source + bilingual note in output EPUB."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import assemble as assemble_module  # type: ignore  # noqa: E402
import state as state_module  # type: ignore  # noqa: E402


def test_aup_refused_chapter_emits_source_with_annotation(tmp_path):
    """Chapter marked aup_refused should appear in output with source text + a
    visible 「本章因 LLM 政策拒答」note injected at the top."""
    # Build a minimal book_dir fixture
    book_dir = tmp_path / "test_book"
    book_dir.mkdir()

    # Minimal manifest.json with one translate chapter
    manifest = {
        "schema_version": "v2",
        "book_path": "fake.epub",
        "spine": [
            {
                "id": "item_001",
                "original_path": "OEBPS/ch01.xhtml",
                "original_idref": "ch01",
                "output_strategy": "translate",
                "html_path": "chapters/item_001.html",
            },
        ],
        "title": "Test Book",
        "author": "Test Author",
    }
    import json
    (book_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    # Source chapter file
    (book_dir / "chapters").mkdir()
    (book_dir / "chapters" / "item_001.html").write_text(
        "<html><body><h1>Ch 1</h1><p>Sensitive content here.</p></body></html>",
        encoding="utf-8",
    )

    # state.json marking item_001 as aup_refused
    state = {
        "book": "fake.epub",
        "started": "2026-05-19T00:00:00Z",
        "target_lang": "zh-tw",
        "glossary_built": True,
        "style_confirmed": True,
        "chapters": {
            "item_001": {
                "output_strategy": "translate",
                "status": "aup_refused",
                "reason": "Anthropic AUP refused this section",
            },
        },
    }
    (book_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    # No translation file — assemble must not raise because of aup_refused status
    # Run the bilingual rewriter directly on this single chapter
    # (we test the unit that decides what to do per chapter, not the full assemble)
    from bilingual_rewriter import insert_bilingual  # type: ignore

    src_html = (book_dir / "chapters" / "item_001.html").read_text(encoding="utf-8")

    # Simulate the entry the assemble pipeline would pass — extend SpineEntry-like dict
    entry = manifest["spine"][0]

    output_html = insert_bilingual(
        src_html=src_html,
        entry=entry,
        translations={},  # no translation; status drives behavior
        chapter_status="aup_refused",
        aup_reason="Anthropic AUP refused this section",
    )

    # Output should keep the source AND include a visible Chinese annotation
    assert "Sensitive content here." in output_html
    assert "本章因" in output_html or "AUP" in output_html
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /Users/fredchu/.claude/skills/book-translator
python3 -m pytest test/test_aup_refused.py -v
```

Expected: fail because `insert_bilingual` does not yet accept `chapter_status` / `aup_reason` kwargs.

- [ ] **Step 3: Modify `scripts/bilingual_rewriter.py`**

Find the `insert_bilingual` signature and add the two new optional kwargs:

```python
def insert_bilingual(
    src_html: str,
    entry,  # dict or SpineEntry
    translations: dict[int, str],
    *,
    chapter_status: str | None = None,
    aup_reason: str | None = None,
) -> str:
    """Per-paragraph interleave src + translation. If chapter_status='aup_refused',
    return source HTML with a visible annotation block prepended; do not insert
    translation siblings."""
    if chapter_status == "aup_refused":
        return _emit_aup_refused_chapter(src_html, aup_reason or "")
    # ... existing implementation ...
```

Add the helper:

```python
_AUP_NOTE_HTML_TEMPLATE = (
    '<div class="aup-refused-note" '
    'style="border-left: 4px solid #c44; padding: 0.5em 1em; margin: 1em 0; '
    'background: #fff4f0; font-size: 0.9em;">'
    '<strong>本章因 LLM 政策拒答，保留原文未譯</strong><br/>'
    'This chapter was refused by the translation LLM\'s usage policy; '
    'the original English is preserved verbatim. '
    '<em>Reason: {reason}</em>'
    '</div>'
)


def _emit_aup_refused_chapter(src_html: str, reason: str) -> str:
    """Inject an AUP-refused notice at the top of <body> and return the result."""
    from bs4 import BeautifulSoup
    safe_reason = (reason or "(no reason recorded)").replace("<", "&lt;").replace(">", "&gt;")
    note = _AUP_NOTE_HTML_TEMPLATE.format(reason=safe_reason)
    soup = BeautifulSoup(src_html, "html.parser")
    body = soup.find("body")
    if body is None:
        return note + src_html
    body.insert(0, BeautifulSoup(note, "html.parser"))
    return str(soup)
```

- [ ] **Step 4: Wire `chapter_status` through from `assemble.py`**

In `scripts/assemble.py`, locate the per-spine-entry rewrite loop. Where it calls `insert_bilingual(...)`, pass the chapter's status from state.json:

```python
# inside the per-entry rewrite loop in assemble()
chapter_status = state.get("chapters", {}).get(entry.id, {}).get("status")
aup_reason = state.get("chapters", {}).get(entry.id, {}).get("reason") \
    if chapter_status == "aup_refused" else None

rewritten = insert_bilingual(
    src_html=src_html,
    entry=entry,
    translations=translations,
    chapter_status=chapter_status,
    aup_reason=aup_reason,
)
```

Also: the existing assemble.py "fail-closed" rule says any `translate` item missing its `item_NNN_translation.txt` is a hard error. Add an exception for `aup_refused`:

```python
if entry.output_strategy == "translate" and chapter_status != "aup_refused":
    if not translation_file.exists():
        raise FileNotFoundError(
            f"translate item {entry.id} missing translation file {translation_file}"
        )
```

- [ ] **Step 5: Run tests**

```bash
cd /Users/fredchu/.claude/skills/book-translator
python3 -m pytest test/test_aup_refused.py test/test_bilingual_rewriter.py test/test_assemble.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/fredchu/.claude/skills/book-translator
git add scripts/bilingual_rewriter.py scripts/assemble.py test/test_aup_refused.py
git commit -m "$(cat <<'EOF'
feat(assemble): emit aup_refused chapters with source + annotation

When state.json marks a chapter aup_refused, the bilingual rewriter prepends a
visible AUP notice block to <body> and returns the source HTML unchanged — no
translation siblings inserted. assemble.py threads the chapter status from
state.json into insert_bilingual and skips the missing-translation hard error
for aup_refused chapters.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Task 8: Teach audits to accept `aup_refused` chapters

**Files:**
- Modify: `~/.claude/skills/book-translator/scripts/translation_quality_audit.py`
- Modify: `~/.claude/skills/book-translator/scripts/bilingual_coverage_audit.py`
- Modify: `~/.claude/skills/book-translator/scripts/structural_audit.py`
- Modify: existing test files for each

- [ ] **Step 1: Write the failing tests**

Append to `test/test_translation_quality_audit.py`:

```python
def test_aup_refused_chapter_does_not_fail_translation_quality_audit(tmp_path):
    """A chapter containing the AUP refused note + source-only paragraphs
    should not trip the translation-quality audit's 'unlisted source-only
    paragraph' or 'too-short translation' checks."""
    # Build a minimal EPUB fixture with one chapter that has the AUP note block
    # ... (test scaffolding — write an EPUB zip with one chapter)
    epub_path = _build_minimal_aup_epub(tmp_path)
    from translation_quality_audit import audit
    ok, failures = audit(epub_path=str(epub_path))
    assert ok, f"audit unexpectedly failed: {failures}"
```

Helper `_build_minimal_aup_epub` should construct an EPUB with a chapter whose HTML matches the output of `_emit_aup_refused_chapter`. Implement the helper in the test file.

Similarly add to `test/test_bilingual_coverage_audit.py`:

```python
def test_aup_refused_chapter_does_not_fail_bilingual_coverage(tmp_path):
    epub_path = _build_minimal_aup_epub(tmp_path)
    from bilingual_coverage_audit import audit
    ok, failures = audit(epub_path=str(epub_path))
    assert ok, f"audit unexpectedly failed: {failures}"
```

And `test/test_structural_audit.py`:

```python
def test_aup_refused_chapter_is_a_valid_state(tmp_path):
    """state.json with aup_refused entries is structurally valid."""
    state = {
        "book": "x.epub", "started": "2026-05-19T00:00:00Z", "target_lang": "zh-tw",
        "glossary_built": True, "style_confirmed": True,
        "chapters": {
            "item_001": {"output_strategy": "translate", "status": "aup_refused",
                         "reason": "refused"},
        },
    }
    # Whatever check structural_audit does on state shape, aup_refused must pass.
    # If structural_audit reads state.json directly, set up a minimal book_dir;
    # if it only validates the state schema via state.py, that's already covered
    # by Task 6 tests.
```

- [ ] **Step 2: Run tests to verify failure**

```bash
cd /Users/fredchu/.claude/skills/book-translator
python3 -m pytest test/test_translation_quality_audit.py test/test_bilingual_coverage_audit.py test/test_structural_audit.py -k aup -v
```

Expected: at least the coverage / translation-quality audits fail because they currently see the AUP note's English paragraphs as "untranslated content paragraph" violations.

- [ ] **Step 3: Modify the audits**

In `translation_quality_audit.py` and `bilingual_coverage_audit.py`: add a chapter-level skip rule. A chapter is exempt from per-paragraph checks if its body contains a `<div class="aup-refused-note">` block (matchable by HTML class). Modify the chapter walker to short-circuit for those chapters:

```python
# At the top of the per-chapter loop, after parsing chapter HTML:
if soup.find("div", class_="aup-refused-note"):
    # AUP-refused chapter — source is intentionally preserved; skip
    continue
```

For `structural_audit.py`: ensure it does not treat `aup_refused` chapters as missing translation. Look for any place that loads state.json + iterates `chapters`; check for `aup_refused` alongside `done` / `source_ready` / `dropped` in the "fine to ship" set.

- [ ] **Step 4: Run all audit tests**

```bash
cd /Users/fredchu/.claude/skills/book-translator
python3 -m pytest test/test_translation_quality_audit.py test/test_bilingual_coverage_audit.py test/test_structural_audit.py test/test_audit_suite.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/fredchu/.claude/skills/book-translator
git add scripts/translation_quality_audit.py scripts/bilingual_coverage_audit.py scripts/structural_audit.py test/test_translation_quality_audit.py test/test_bilingual_coverage_audit.py test/test_structural_audit.py
git commit -m "$(cat <<'EOF'
feat(audits): accept aup_refused chapters as valid

Translation-quality + bilingual-coverage audits short-circuit on chapters
containing the <div class='aup-refused-note'> block — those are by design
source-preserved. Structural audit treats aup_refused alongside done /
source_ready / dropped as a 'fine to ship' terminal state.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase 3: Subagent Prompt Purity (already partly in Phase 1)

> Phase 1's `validate_translation` already added the preface-leak check. This phase strengthens the prompt itself and adds an output-side stripper for cases where the leak slips through.

### Task 9: Add `strip_known_leak_prefixes` helper

**Files:**
- Modify: `~/.claude/skills/book-translator/scripts/dispatch.py`
- Modify: `~/.claude/skills/book-translator/test/test_dispatch.py`

- [ ] **Step 1: Write the failing test**

Append to `test/test_dispatch.py`:

```python
def test_strip_known_leak_prefixes_removes_here_is_translation():
    raw = "Here is the translation:\n\n[[PARA_1]]\n甲"
    cleaned = dispatch.strip_known_leak_prefixes(raw)
    assert cleaned.startswith("[[PARA_1]]")


def test_strip_known_leak_prefixes_removes_markdown_fence():
    raw = "```\n[[PARA_1]]\n甲\n```"
    cleaned = dispatch.strip_known_leak_prefixes(raw)
    assert "```" not in cleaned
    assert cleaned.startswith("[[PARA_1]]")


def test_strip_known_leak_prefixes_leaves_clean_output_unchanged():
    raw = "[[PARA_1]]\n甲\n\n[[PARA_2]]\n乙"
    assert dispatch.strip_known_leak_prefixes(raw) == raw
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /Users/fredchu/.claude/skills/book-translator
python3 -m pytest test/test_dispatch.py::test_strip_known_leak_prefixes_removes_here_is_translation -v
```

Expected: `AttributeError`.

- [ ] **Step 3: Modify `dispatch.py`**

Add the helper:

```python
_LEAK_PREFIX_PATTERNS = [
    re.compile(r"^Here\s+(is|are)\s+(the|your)?\s*translation[^\n]*\n+", re.IGNORECASE),
    re.compile(r"^Translation\s*:\s*\n+", re.IGNORECASE),
    re.compile(r"^Sure[,!]?\s+[^\n]*\n+", re.IGNORECASE),
    re.compile(r"^Okay[,!]?\s+[^\n]*\n+", re.IGNORECASE),
    re.compile(r"^I'll\s+translate[^\n]*\n+", re.IGNORECASE),
]

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*\n(.*?)\n```\s*$", re.DOTALL)


def strip_known_leak_prefixes(raw: str) -> str:
    """Remove known leak prefixes and markdown fences from a subagent response.

    Idempotent — if the response is already clean, returns it unchanged.
    """
    text = (raw or "").lstrip()
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
```

- [ ] **Step 4: Wire `strip_known_leak_prefixes` into `extract_aligned_translation`**

Modify `extract_aligned_translation` to call the stripper first:

```python
def extract_aligned_translation(raw_output: str, expected_count: int) -> str:
    cleaned = strip_known_leak_prefixes(raw_output)
    result = ma.parse_marker_output(cleaned, expected_count=expected_count)
    if result.missing_markers:
        raise ValueError(f"missing markers in subagent output: {result.missing_markers}")
    if result.extra_markers:
        raise ValueError(f"extra markers in subagent output: {result.extra_markers}")
    return "\n\n".join(result.translations)
```

- [ ] **Step 5: Run all dispatch tests**

```bash
cd /Users/fredchu/.claude/skills/book-translator
python3 -m pytest test/test_dispatch.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/fredchu/.claude/skills/book-translator
git add scripts/dispatch.py test/test_dispatch.py
git commit -m "$(cat <<'EOF'
feat(dispatch): strip_known_leak_prefixes + fold into extract_aligned_translation

Drop preface phrases (Here is the translation:, Translation:, Sure, ...) and
markdown fences from subagent output before marker parsing. Idempotent on
already-clean output. extract_aligned_translation now applies the stripper
first so callers get clean translation text even if the subagent ignored the
'output must start with [[PARA_1]]' rule.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Task 10: Detect AUP refusal in subagent output

**Files:**
- Modify: `~/.claude/skills/book-translator/scripts/dispatch.py`
- Modify: `~/.claude/skills/book-translator/test/test_dispatch.py`

- [ ] **Step 1: Write the failing test**

Append to `test/test_dispatch.py`:

```python
def test_detect_aup_refusal_returns_reason_on_known_phrases():
    cases = [
        "I cannot help with translating copyrighted material.",
        "I'm unable to provide a translation of this text because it appears to be from a copyrighted work.",
        "I won't be able to assist with this task.",
        "As an AI, I cannot reproduce this content.",
    ]
    for raw in cases:
        reason = dispatch.detect_aup_refusal(raw)
        assert reason is not None, f"failed to detect: {raw}"
        assert len(reason) > 0


def test_detect_aup_refusal_returns_none_for_clean_translation():
    raw = "[[PARA_1]]\n甲\n\n[[PARA_2]]\n乙"
    assert dispatch.detect_aup_refusal(raw) is None
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /Users/fredchu/.claude/skills/book-translator
python3 -m pytest test/test_dispatch.py::test_detect_aup_refusal_returns_reason_on_known_phrases -v
```

Expected: `AttributeError`.

- [ ] **Step 3: Modify `dispatch.py`**

Add the detector:

```python
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
```

Also: have `validate_translation` surface AUP refusal explicitly so callers can route to `state.mark_aup_refused`:

```python
def validate_translation(translation: str, chapter_html: str, *, min_ratio: float = 0.5) -> list[str]:
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

    # ... rest of the existing function ...
```

- [ ] **Step 4: Run all dispatch tests**

```bash
cd /Users/fredchu/.claude/skills/book-translator
python3 -m pytest test/test_dispatch.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/fredchu/.claude/skills/book-translator
git add scripts/dispatch.py test/test_dispatch.py
git commit -m "$(cat <<'EOF'
feat(dispatch): detect_aup_refusal + flag in validate_translation

When a subagent response trips known AUP refusal phrases ('I cannot',
'I'm unable to', 'violate our usage policy', etc.), validate_translation
returns a single AUP_REFUSED warning so the caller can route to
state.mark_aup_refused instead of marking failed/retryable.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Task 11: Update SKILL.md retry/escalation flow

**Files:**
- Modify: `~/.claude/skills/book-translator/SKILL.md`

- [ ] **Step 1: Locate retry section**

```bash
grep -n "retry" /Users/fredchu/.claude/skills/book-translator/SKILL.md | head -10
```

- [ ] **Step 2: Add a short subsection after Step 6**

Add this block under "### Step 6: ...":

```markdown
#### Retry / escalation discipline (post Phase 1-3 upgrades)

After each subagent returns, the main session runs:

1. `warnings = dispatch.validate_translation(raw, chapter_html)`
2. If `warnings` contains an entry starting with `AUP_REFUSED:` →
   `state.mark_aup_refused(state, chapter_id, reason=warnings[0])`. Do not retry.
3. If `warnings` is empty → `dispatch.extract_aligned_translation(raw, N)` →
   `state.mark_done(state, chapter_id, text)`.
4. If `warnings` contains marker misalignment OR refusal language (other than
   AUP) OR preface leak → retry **once** with the same subagent prompt.
5. If retry still fails → escalate: re-dispatch with `model: "opus"` (Phase 4).
6. If escalation also fails → `state.mark_failed(state, chapter_id, error)`.

Marker misalignment is the most common warning; preface leak is usually fixed
by `strip_known_leak_prefixes` before the marker parse, so it rarely surfaces
as a real warning unless the leak text contained no marker at all.
```

- [ ] **Step 3: Commit**

```bash
cd /Users/fredchu/.claude/skills/book-translator
git add SKILL.md
git commit -m "$(cat <<'EOF'
docs(skill): retry / escalation discipline post Phase 1-3 upgrades

Document the 6-step decision tree the main session runs after each subagent
return: validate → AUP route / extract / retry / escalate to Opus / mark failed.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase 4: Default Model = Sonnet (Opus Escalation)

> No new code — this is a config + docs change. The model choice is passed by the main session through the `Agent` tool's `model:` field; we document the discipline.

### Task 12: Add "Model selection discipline" section to SKILL.md

**Files:**
- Modify: `~/.claude/skills/book-translator/SKILL.md`

- [ ] **Step 1: Update the description in the frontmatter**

In `SKILL.md`'s frontmatter `description:`, replace:

```
... book-translator targets literary works specifically, uses Opus 4.7, gates
translation quality with cross-modal eval ...
```

with:

```
... book-translator targets literary works specifically, uses Opus 4.7 for the
ch.01 style-anchor pass and Sonnet 4.6 for the parallel ch.02+ fan-out (with
Opus escalation on validation reject), gates translation quality with
cross-modal eval ...
```

- [ ] **Step 2: Add a "Model selection discipline" section**

Insert after the "### 4 Coherence Mechanisms" subsection:

```markdown
### Model selection discipline

| Stage | Model | Why |
|---|---|---|
| Glossary build (full-book read) | Sonnet 4.6 | Throughput; the LLM is summarizing structured fields, not producing literary text. |
| Ch.01 style sample (main session) | **Opus 4.7** | The translation here becomes the style anchor for every subsequent subagent. Pay for quality once. |
| Ch.02..ch.N fan-out (subagent batch) | **Sonnet 4.6** (default) | Bulk throughput. The style anchor + glossary + carryover constrain Sonnet's register adequately for business / popular-science / general non-fiction books. |
| Escalation on validation reject | Opus 4.7 | Retry with the same prompt but better model when Sonnet fails marker alignment, leaks persona, or produces refusal language (non-AUP). |
| Spot-check pass (random 5 paragraphs) | Sonnet 4.6 | Lightweight read; main session calls inline. |

**Why not Opus everywhere?** Token cost. Bocky's 9-book实战 baseline (Opus
default) ran at ~12h / book because 100+ chunks × Opus eats the 5h Anthropic
quota window 2-3 times per book. Sonnet for fan-out cuts the token cost ~5x
without dropping below the literary-tone-fidelity floor for our scope
(business / popular-science). For literary fiction or
philosophy-heavy texts (Taleb / Mandelbrot register), override to Opus default
via `model: "opus"` on the Agent dispatch.

**How to override per book**: the orchestrating session sets `model:` on the
Agent tool call. There is no config file — model selection is a per-dispatch
decision the main session makes based on the book's domain and the user's
quality preference for that batch.
```

- [ ] **Step 3: Commit**

```bash
cd /Users/fredchu/.claude/skills/book-translator
git add SKILL.md
git commit -m "$(cat <<'EOF'
docs(skill): Sonnet default for fan-out, Opus for ch.01 anchor + escalation

Document the model discipline post Phase 1-3 marker / AUP / purity upgrades:
ch.01 in main session uses Opus (style anchor), ch.02+ subagent fan-out uses
Sonnet (throughput), validation rejects escalate to Opus retry. Override per
book by setting model on the Agent tool call.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Task 13: Update existing SKILL.md prose that says "Opus 4.7" generically

**Files:**
- Modify: `~/.claude/skills/book-translator/SKILL.md`

- [ ] **Step 1: Find all instances**

```bash
grep -n "Opus 4.7\|opus" /Users/fredchu/.claude/skills/book-translator/SKILL.md
```

- [ ] **Step 2: Tighten each reference**

Edit each line where "Opus 4.7" appears in a context that implies "all subagents":
- Step 6 example: `model: "sonnet"` for ch.02+ (already changed in Task 5)
- "Differentiation from translate-book" table row "Subagent model": replace `Opus` with `Sonnet (Opus for ch.01 + escalation)`
- Architecture diagram comment: clarify ch.01 main session = Opus, ch.02+ = Sonnet

Leave Opus references untouched in: the legal disclaimer, the cross-modal eval section (Slot A model), and the ch.01 style-sample section.

- [ ] **Step 3: Commit**

```bash
cd /Users/fredchu/.claude/skills/book-translator
git add SKILL.md
git commit -m "$(cat <<'EOF'
docs(skill): clarify Sonnet (fan-out) vs Opus (anchor / escalation) throughout

Sweep SKILL.md for generic 'Opus 4.7' mentions and disambiguate per stage.
Cross-modal eval and the legal disclaimer keep Opus references unchanged.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase 5: Per-Chapter Translation Log for Replay

### Task 14: Build `translation_log.py`

**Files:**
- Create: `~/.claude/skills/book-translator/scripts/translation_log.py`
- Create: `~/.claude/skills/book-translator/test/test_translation_log.py`

- [ ] **Step 1: Write the failing test**

Write `test/test_translation_log.py`:

```python
"""Tests for translation_log — per-chapter prompt+response persistence."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import translation_log as tlog  # type: ignore  # noqa: E402


def test_write_log_entry_creates_translation_log_dir(tmp_path):
    tlog.write_log_entry(
        book_dir=tmp_path,
        chapter_id="item_002",
        prompt="prompt text",
        raw_response="[[PARA_1]]\n甲",
        parsed_translation="甲",
        validation_warnings=[],
        model="sonnet",
    )
    log_dir = tmp_path / "translation_log"
    assert log_dir.is_dir()
    entry_path = log_dir / "item_002.json"
    assert entry_path.exists()


def test_write_log_entry_payload_round_trips(tmp_path):
    tlog.write_log_entry(
        book_dir=tmp_path,
        chapter_id="item_005",
        prompt="P",
        raw_response="R",
        parsed_translation="T",
        validation_warnings=["w1"],
        model="opus",
    )
    entry = tlog.read_log_entry(book_dir=tmp_path, chapter_id="item_005")
    assert entry["chapter_id"] == "item_005"
    assert entry["model"] == "opus"
    assert entry["prompt"] == "P"
    assert entry["raw_response"] == "R"
    assert entry["parsed_translation"] == "T"
    assert entry["validation_warnings"] == ["w1"]
    assert "timestamp" in entry


def test_write_log_entry_records_marker_count_when_aligned(tmp_path):
    raw = "[[PARA_1]]\n甲\n\n[[PARA_2]]\n乙"
    tlog.write_log_entry(
        book_dir=tmp_path, chapter_id="item_003", prompt="P", raw_response=raw,
        parsed_translation="甲\n\n乙", validation_warnings=[],
        model="sonnet", source_paragraph_count=2,
    )
    entry = tlog.read_log_entry(book_dir=tmp_path, chapter_id="item_003")
    assert entry["source_paragraph_count"] == 2
    assert entry.get("response_marker_count") == 2


def test_read_log_entry_returns_none_when_missing(tmp_path):
    assert tlog.read_log_entry(book_dir=tmp_path, chapter_id="nope") is None
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /Users/fredchu/.claude/skills/book-translator
python3 -m pytest test/test_translation_log.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write the module**

Write `scripts/translation_log.py`:

```python
"""Per-chapter translation log — full prompt + raw response + parsed output.

One JSON file per chapter under `<book_dir>/translation_log/<chapter_id>.json`.
Lets the main session replay a chapter's translation offline (without re-
dispatching the subagent) when an audit catches a regression or the user
wants to inspect what the LLM actually saw vs returned.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

_MARKER_COUNT_RE = re.compile(r"\[\[\s*PARA[\s_]*\d+\s*\]\]", re.IGNORECASE)


def _log_dir(book_dir: Path) -> Path:
    return Path(book_dir) / "translation_log"


def write_log_entry(
    *,
    book_dir: Path,
    chapter_id: str,
    prompt: str,
    raw_response: str,
    parsed_translation: str,
    validation_warnings: list[str],
    model: str,
    source_paragraph_count: int | None = None,
) -> Path:
    """Write a per-chapter log entry. Overwrites any prior entry for the same id."""
    log_dir = _log_dir(book_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "chapter_id": chapter_id,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": model,
        "prompt": prompt,
        "raw_response": raw_response,
        "parsed_translation": parsed_translation,
        "validation_warnings": validation_warnings,
    }
    if source_paragraph_count is not None:
        entry["source_paragraph_count"] = source_paragraph_count
        entry["response_marker_count"] = len(_MARKER_COUNT_RE.findall(raw_response or ""))
    path = log_dir / f"{chapter_id}.json"
    path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_log_entry(*, book_dir: Path, chapter_id: str) -> dict | None:
    path = _log_dir(book_dir) / f"{chapter_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_logged_chapters(book_dir: Path) -> list[str]:
    log_dir = _log_dir(book_dir)
    if not log_dir.is_dir():
        return []
    return sorted(p.stem for p in log_dir.glob("*.json"))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/fredchu/.claude/skills/book-translator
python3 -m pytest test/test_translation_log.py -v
```

Expected: all 4 tests pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/fredchu/.claude/skills/book-translator
git add scripts/translation_log.py test/test_translation_log.py
git commit -m "$(cat <<'EOF'
feat(translation-log): per-chapter prompt + raw + parsed log

Write one JSON per chapter under <book_dir>/translation_log/. Captures the
subagent prompt, raw response, parsed translation, validation warnings, model,
source paragraph count, and response marker count. Enables offline replay
when an audit catches a regression.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Task 15: Build `replay_chapter.py` CLI

**Files:**
- Create: `~/.claude/skills/book-translator/scripts/replay_chapter.py`

- [ ] **Step 1: Write the script**

Write `scripts/replay_chapter.py`:

```python
#!/usr/bin/env python3
"""CLI: re-validate + re-extract a logged chapter's translation without
re-dispatching the subagent.

Usage:
    python3 replay_chapter.py --book-dir <dir> --chapter <chapter_id>
    python3 replay_chapter.py --book-dir <dir> --chapter <chapter_id> \\
        --rewrite-translation-file

Without --rewrite-translation-file, prints validation warnings and the parsed
translation to stdout for human review. With the flag, also writes the parsed
translation to `<book_dir>/chapters/<chapter_id>_translation.txt` so a later
assemble.py run picks it up.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import dispatch  # type: ignore
import translation_log as tlog  # type: ignore


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--book-dir", required=True, type=Path)
    p.add_argument("--chapter", required=True, help="chapter_id, e.g. item_005")
    p.add_argument("--rewrite-translation-file", action="store_true")
    args = p.parse_args()

    entry = tlog.read_log_entry(book_dir=args.book_dir, chapter_id=args.chapter)
    if entry is None:
        print(f"No log entry for {args.chapter} in {args.book_dir}/translation_log/",
              file=sys.stderr)
        return 1

    print(f"Chapter: {entry['chapter_id']}")
    print(f"Model:   {entry['model']}")
    print(f"Time:    {entry['timestamp']}")
    print()

    raw = entry["raw_response"]
    expected = entry.get("source_paragraph_count")
    if expected is None:
        print("WARN: log entry has no source_paragraph_count; using marker count as expected.",
              file=sys.stderr)
        expected = entry.get("response_marker_count", 0)

    cleaned = dispatch.strip_known_leak_prefixes(raw)
    try:
        text = dispatch.extract_aligned_translation(cleaned, expected_count=expected)
        print("[OK] Marker alignment passed.")
        print(f"[OK] {len(text.split(chr(10) + chr(10)))} paragraphs in parsed translation.")
    except ValueError as e:
        print(f"[FAIL] {e}")
        print("Use the log file directly to repair the raw response.")
        return 2

    # AUP refusal check
    aup = dispatch.detect_aup_refusal(raw)
    if aup:
        print(f"[NOTE] AUP refusal phrase still present: {aup}")

    if args.rewrite_translation_file:
        out_path = Path(args.book_dir) / "chapters" / f"{args.chapter}_translation.txt"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"[WROTE] {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Smoke test with a fixture**

```bash
cd /Users/fredchu/.claude/skills/book-translator
python3 scripts/replay_chapter.py --help
```

Expected: prints usage, exits 0.

For a full end-to-end smoke test, build a tiny book_dir with a translation_log and run it:

```bash
mkdir -p /tmp/bt_replay_smoke/translation_log
cat > /tmp/bt_replay_smoke/translation_log/item_001.json <<'EOF'
{
  "chapter_id": "item_001",
  "timestamp": "2026-05-19T00:00:00Z",
  "model": "sonnet",
  "prompt": "...",
  "raw_response": "[[PARA_1]]\n甲\n\n[[PARA_2]]\n乙",
  "parsed_translation": "甲\n\n乙",
  "validation_warnings": [],
  "source_paragraph_count": 2,
  "response_marker_count": 2
}
EOF
python3 scripts/replay_chapter.py --book-dir /tmp/bt_replay_smoke --chapter item_001
```

Expected: `[OK] Marker alignment passed.` + paragraph count line.

- [ ] **Step 3: Commit**

```bash
cd /Users/fredchu/.claude/skills/book-translator
git add scripts/replay_chapter.py
git commit -m "$(cat <<'EOF'
feat(replay-chapter): offline CLI to re-validate logged chapter

Read a logged chapter's raw response, run it through strip_known_leak_prefixes
+ extract_aligned_translation, print validation result. With
--rewrite-translation-file, also writes the parsed text to
<book_dir>/chapters/<chapter_id>_translation.txt so assemble.py picks it up
on the next run.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Task 16: Document the log in SKILL.md

**Files:**
- Modify: `~/.claude/skills/book-translator/SKILL.md`

- [ ] **Step 1: Add a section after Step 9 (Translation spot-check)**

```markdown
### Step 10: Per-chapter translation log

After each subagent return + validation, the main session calls
`translation_log.write_log_entry(...)` to persist the full prompt, raw
response, parsed translation, validation warnings, model, and marker counts
under `<book_dir>/translation_log/<chapter_id>.json`.

When an audit catches a regression in a single chapter, repair it offline
without re-dispatching the subagent:

```bash
python3 ~/.claude/skills/book-translator/scripts/replay_chapter.py \
    --book-dir <out_dir>/<book_stem> \
    --chapter item_007 \
    --rewrite-translation-file
```

Then re-run `assemble.py` on the same `book_dir`; the rewritten translation
file will be picked up.
```

Also update the "Output" section to list `translation_log/` among the per-book outputs.

- [ ] **Step 2: Commit**

```bash
cd /Users/fredchu/.claude/skills/book-translator
git add SKILL.md
git commit -m "$(cat <<'EOF'
docs(skill): document translation_log + replay_chapter workflow

Add Step 10 explaining the per-chapter JSON log and the offline replay CLI.
Update the Output section to list translation_log/ as a per-book artifact.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase 6: Final integration + release

### Task 17: Full e2e test + CHANGELOG

**Files:**
- Modify: `~/.claude/skills/book-translator/test/test_e2e.py` (run, don't necessarily edit)
- Modify: `~/.claude/skills/book-translator/CHANGELOG.md`

- [ ] **Step 1: Run the full test suite**

```bash
cd /Users/fredchu/.claude/skills/book-translator
python3 -m pytest test/ -v
```

Expected: all tests pass. If any pre-existing test fails because of the marker change, **fix the test if it's checking the old `\n\n` paragraph contract** (the new marker contract supersedes it) — but do not weaken any audit assertion.

- [ ] **Step 2: Run the animal_farm e2e fixture**

```bash
cd /Users/fredchu/.claude/skills/book-translator
python3 -m pytest test/test_e2e.py -v
```

Expected: pass (this test exercises extract → assemble → 4 audits on the public-domain fixture without making real LLM calls).

- [ ] **Step 3: Append to CHANGELOG.md `[Unreleased]` section**

```markdown
### Added
- `scripts/marker_alignment.py` — `[[PARA_N]]` source-side wrap +
  output-side parser for structural verification of subagent translations.
- `scripts/translation_log.py` — per-chapter `<book_dir>/translation_log/<chapter_id>.json`
  capture of full prompt + raw response + parsed translation + validation warnings.
- `scripts/replay_chapter.py` — offline CLI to re-validate + re-extract a logged
  chapter's translation without re-dispatching the subagent.
- `state.py` adds `aup_refused` status + `mark_aup_refused` helper. Terminal
  state for chapters refused by Anthropic AUP; output preserves source with
  an HTML annotation block instead of erroring out the whole book.
- `dispatch.strip_known_leak_prefixes` — removes preface phrases ("Here is
  the translation:", markdown fences, etc.) before marker parsing.
- `dispatch.detect_aup_refusal` — recognizes common AUP refusal phrases in
  the response so callers can route to `mark_aup_refused` instead of `mark_failed`.
- `dispatch.extract_aligned_translation` — strip-and-parse helper that
  returns plain chapter text from a marker-aligned response.

### Changed
- `dispatch.SUBAGENT_PROMPT_TEMPLATE` — source paragraphs now arrive wrapped
  with `[[PARA_1]]..[[PARA_N]]` markers. The subagent must echo every marker
  followed by its translation and start its output with `[[PARA_1]]` (no
  preface). Verification rule asks the subagent to count markers before
  returning.
- `dispatch.validate_translation` — primary path is marker alignment
  (missing/extra markers reported); legacy paragraph-ratio path retained for
  responses without markers. Also surfaces `AUP_REFUSED:` warning and preface
  leak.
- `assemble.insert_bilingual` accepts `chapter_status` + `aup_reason` kwargs.
  `aup_refused` chapters get a visible `<div class="aup-refused-note">`
  injected at top of `<body>`; source preserved verbatim, no translation
  siblings.
- `translation_quality_audit` + `bilingual_coverage_audit` short-circuit on
  chapters containing the aup-refused note.
- `structural_audit` accepts `aup_refused` alongside `done` / `source_ready` /
  `dropped` as a valid terminal state for ship-ready chapters.
- SKILL.md: model discipline now documents Sonnet 4.6 default for ch.02+
  fan-out subagents, Opus 4.7 for ch.01 style sample + escalation on
  validation reject. Cross-modal eval slot still uses Opus.
```

- [ ] **Step 4: Commit**

```bash
cd /Users/fredchu/.claude/skills/book-translator
git add CHANGELOG.md
git commit -m "$(cat <<'EOF'
docs(changelog): unreleased — marker alignment + AUP fallback + replay log

Document Phase 1-5 changes:
- [[PARA_N]] marker alignment (dispatch.py + marker_alignment.py)
- aup_refused status + assembly behavior
- strip_known_leak_prefixes + detect_aup_refusal in dispatch
- translation_log + replay_chapter CLI
- Sonnet default for fan-out, Opus for anchor + escalation

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Task 18: Tag a pre-release and push

**Files:**
- Modify: `~/.claude/skills/book-translator/SKILL.md` (frontmatter version, if there is one)
- Modify: `~/.claude/skills/book-translator/CHANGELOG.md` (promote Unreleased → version)

- [ ] **Step 1: Decide version number**

Look at recent tags:
```bash
cd /Users/fredchu/.claude/skills/book-translator
git tag --sort=-v:refname | head -5
```

Per MEMORY.md `feedback_release_bump_frontmatter.md`, frontmatter version must bump if present. Check:
```bash
head -20 SKILL.md
```

If frontmatter has `version:` — bump per semver. The Phase 1-5 changes are additive + behavior changes to existing functions = MINOR bump. If current is `v0.X.Y` → `v0.(X+1).0`.

- [ ] **Step 2: Promote CHANGELOG section**

In `CHANGELOG.md`, rename `[Unreleased]` to `[v0.X.0] - 2026-05-19` (substitute real version + today's date). Add a fresh empty `[Unreleased]` section above it.

- [ ] **Step 3: Bump frontmatter version if applicable**

If `SKILL.md` frontmatter has `version:`, update to match.

- [ ] **Step 4: Commit + tag**

```bash
cd /Users/fredchu/.claude/skills/book-translator
git add CHANGELOG.md SKILL.md
git commit -m "$(cat <<'EOF'
release: v0.X.0 — marker alignment + AUP fallback + replay log

See CHANGELOG.md [v0.X.0] for full list.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git tag v0.X.0
```

- [ ] **Step 5: Push**

```bash
cd /Users/fredchu/.claude/skills/book-translator
git push origin main --tags
```

Do not force-push.

- [ ] **Step 6: Verify upgrade in a fresh CC session**

User opens a new Pro CC session and runs `/reload-plugins` (per MEMORY.md hot-reload note). Then ask CC to print the SKILL.md description — should reflect the new "Sonnet 4.6 for the parallel ch.02+ fan-out" wording.

---

## Out of Scope (next-next round)

- True per-chunk Agent dispatch (1500-token chunks within a chapter, each = its own subagent call). Currently chunking lives only in the subagent's own context window once it receives a marker-wrapped chapter. This plan does NOT add a main-session loop that dispatches N subagent calls per chapter.
- Main-session interactive intervention during ch.02+ subagent runs (the in-flight "pause and correct" UX from the original discussion). The current best intervention surface is: main session sees subagent return, validates, optionally retries; for chunk-level intervention the architecture would need to be redesigned.
- Cross-book shared glossary.
- Configurable concurrency below 5 / above 5. The Agent tool's concurrency stays at the SKILL.md default.
- Discord / Slack notifications on chapter completion.

---

## Self-review checklist

**1. Spec coverage** — the user's 5 design points from session discussion:

1. Marker alignment (`[[PARA_N]]`) → ✅ Phase 1 (Tasks 1-5)
2. AUP refuse fallback (preserve source + annotation) → ✅ Phase 2 (Tasks 6-8)
3. Subagent prompt purity (strip leak, detect AUP) → ✅ Phase 3 (Tasks 9-11)
4. Sonnet default + Opus escalation → ✅ Phase 4 (Tasks 12-13)
5. Per-chunk JSON log → ✅ Phase 5 chapter-level (Tasks 14-16). Per-chunk log NOT done (chunk dispatch is out of scope this round); per-chapter log gives most of the replay benefit.

User's quality concerns (from earlier comparison):
- 漏譯 → Marker enforcement + missing-marker detection (Phase 1) ✅
- 格式錯誤 → Marker contract + preface leak stripping (Phase 1 + 3) ✅
- 30% copyright refuse → AUP fallback path (Phase 2 + 3) ✅
- token 省 → Sonnet default fan-out (Phase 4) ✅
- 時間短 → Sonnet 5x faster than Opus + fewer retries from cleaner output (Phase 1+4 compound) ✅
- 主 session 跑 → Not addressed this round (chunk-dispatch out of scope). Main session still drives orchestration but per-chunk LLM calls remain inside subagents. Documented in Out of Scope.

**2. Placeholder scan** — no TBD / TODO / "fill in details" / "similar to Task N". Every code block is full. Every commit message has a body. Every test has full assertions.

**3. Type / API consistency**:
- `marker_alignment.parse_marker_output` returns `ParseResult` with fields `translations / translations_by_idx / missing_markers / extra_markers / is_aligned`. Referenced consistently in Tasks 1, 3, 4, 9, 15.
- `dispatch.extract_aligned_translation(raw, expected_count) -> str` — same signature in Tasks 4, 9 (called from `strip_known_leak_prefixes` chain), 15 (replay_chapter).
- `dispatch.detect_aup_refusal(raw) -> str | None` — same in Tasks 10, 11 (SKILL.md retry flow), 15 (replay_chapter).
- `dispatch.strip_known_leak_prefixes(raw) -> str` — same in Tasks 9, 15.
- `dispatch.validate_translation(translation, chapter_html, *, min_ratio=0.5) -> list[str]` — preserved existing signature; added `AUP_REFUSED:` prefix on the AUP warning so callers can pattern-match.
- `state.mark_aup_refused(state, chapter_id, reason)` — module-level helper, Task 6. Referenced in Task 11 (SKILL.md) and Task 8 (audits).
- `bilingual_rewriter.insert_bilingual(src_html, entry, translations, *, chapter_status=None, aup_reason=None) -> str` — Task 7 adds the two kwargs. Tests in Task 7 + Task 8 use both.
- `translation_log.write_log_entry(*, book_dir, chapter_id, prompt, raw_response, parsed_translation, validation_warnings, model, source_paragraph_count=None) -> Path` — same in Tasks 14, 15 (replay).

**4. Spec gaps fixed inline**:
- Caught and fixed: existing test `test_subagent_prompt_template_makes_paragraph_separator_explicit` becomes invalid after marker conversion → replaced inline with `test_subagent_prompt_template_makes_marker_contract_explicit` in Task 2.
- Caught: assemble.py fail-closed rule "translate items must have translation file" would break for aup_refused chapters → exception added in Task 7.

---

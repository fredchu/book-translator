# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `<book_dir>/translations_extra.json` schema for per-book overrides
  (`by_exact_text` and `nav_overrides`). The assembler reads this file when
  present; book-specific dedications, copyright body text, or custom nav labels
  for a specific edition belong here, never in source.
- `assets/register_hints.json` with three generic registers
  (`literary_fiction`, `non_fiction_narrative`, `academic_technical`). The
  glossary prompt loads these at import time and injects them into the LLM
  prompt as abstract style traits — no author names cited.
- Explicit `parent_id` field on `SpineEntry`. The extractor threads each
  body/epilogue/acknowledgments/about_author/notes entry that follows a
  `part_divider` under that divider's id, so PART → chapter nesting is
  data-driven, not heuristic.

### Changed
- Renamed `assemble.ZH_BY_EXACT_TEXT` → `STRUCTURAL_LABELS_ZH_TW` and
  `CONTENTS_LINK_LABELS` → `CONTENTS_LINK_LABELS_ZH_TW`. Both are documented
  as generic structural i18n dicts — well-known English structural labels
  (Contents, Acknowledgments, Notes, Cover, Title Page, etc.) mapped to
  standard 台灣繁體中文. Book-specific content lives in
  `translations_extra.json`, not these dicts.
- `_build_nav_xhtml()` now renders generically from `manifest.spine[]` in
  every case: top-level `<ol>` with nested `<ol>` under each `part_divider`,
  bilingual "English ｜ 繁中" labels derived from
  `translations_extra.nav_overrides` → `STRUCTURAL_LABELS_ZH_TW` →
  `CONTENTS_LINK_LABELS_ZH_TW` → existing fallback.

### Removed
- `scripts/regenerate_bilingual.py`. The "re-assemble a bilingual EPUB from
  existing translated chapter files" workflow is now covered by
  `scripts/assemble.py` (idempotent given the same `book_dir`). The deleted
  file also held ~150 lines of copyrighted paragraph translations and three
  hardcoded user-specific paths — neither of which belong in source.
- `_is_co_intelligence()` and `CO_INTELLIGENCE_NAV_XHTML` from
  `scripts/assemble.py`. Replaced by generic manifest-driven nav rendering.
- Author-name register exemplars from `scripts/glossary.py` prompt template
  (specific writers were referenced as register exemplars; replaced with
  abstract style descriptions).

## [0.1.0-pre] — 2026-05-14

Internal milestone (pre-OSS). Initial extraction from the in-tree skill
implementation into a standalone repository.

### Added
- `scripts/extract_epub.py`: walk the source OPF spine and emit a full
  manifest v2 (every spine item represented, `output_strategy` ∈
  `{translate, source_only, nav_generated, drop_explicit}`).
- `scripts/dispatch.py`: build per-subagent prompts with glossary +
  style anchor + carryover for cross-chunk coherence.
- `scripts/assemble.py`: emit the bilingual EPUB from extracted source
  spine + per-item translation files, preserving original CSS / fonts /
  images / XHTML paths / internal href targets verbatim.
- Five deterministic audit gates (`structural_audit.py`,
  `bilingual_coverage_audit.py`, `href_resolve_audit.py`,
  `translation_quality_audit.py`, plus pytest). Each invariant lives in
  its own script so "audit pass but quality collapse" failure modes are
  separately caught.
- Glossary prompt template + parser + canonical form writer
  (`scripts/glossary.py`).
- Bilingual README (English + 繁中) and `SKILL.md` documenting the
  full workflow.

### Notes
- This release is the basis for the public 1.0.0 cut, after the
  "Unreleased" refactor above lands.

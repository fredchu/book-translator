# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **Offline default model: `Qwopus3.6-27B-v2-MLX-4bit` → `Qwen3.6-35B-Heretic-4bit`**
  (2026-06-25). The new default is a Qwen3.6-35B-A3B 3B-active MoE that generates
  ~5x faster (~43 vs ~8 t/s on M1 Max) at parity Opus-tier register and lower
  Simplified leak (0.3% vs 0.9%) — a 23-chapter book drops from ~2h13m to an
  estimated ~25-30min. `enable_thinking:False` suppresses thinking cleanly via the
  model's fixed chat template, so `OmlxProvider` is unchanged. Qwopus3.6-27B-v2
  remains the documented fallback (smaller RAM footprint). Defaults updated in
  `translate_chapter_cli.py` + `translate_book_ollama.py`; default-contract tests
  updated. Evidence: `company/book-translator/2026-06-25-heretic-35b-a3b-local-translation-speedup.md`.

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
- `scripts/providers/` package — `TranslationProvider` ABC with
  `OllamaProvider` (HTTP client to localhost:11434) and `AnthropicProvider`
  (marker class). Lets the pipeline target a local Ollama server (e.g.
  translategemma:4b/12b/27b) as a CC-quota fallback or offline draft.
- `scripts/translate_chapter_cli.py` — single-chapter CLI used for
  benchmarking and ad-hoc spot-checks; gains `--validate-markers` flag that
  runs Phase 1 marker alignment against the provider output and writes
  `<request_id>.aligned.txt` on success.
- `scripts/run_benchmark.py` — cross-model benchmark CLI that runs N
  models across M chapters, extracts Bocky/Fred bilingual baselines for
  side-by-side review, and emits a `comparison.md` template.

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

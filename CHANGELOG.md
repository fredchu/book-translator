# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `scripts/cloud_llm.sh`: run the omlx engine against a vLLM server on a rented GPU (Vast.ai or RunPod).
  No code upload, no SSH — the instance runs the official `vllm/vllm-openai:v0.28.0` image in args mode
  with port 8000 exposed and `--api-key` protection; the script waits for `/v1/models` to list the model,
  runs `translate_book_ollama.py --engine omlx --omlx-host <endpoint>`, then destroys the instance
  (trap on every exit path, list re-checked). `--profile int4` (default,
  `XReyRobert/Qwopus3.6-27B-v2-GPTQ-Pro-v1` on RTX 5090) / `--profile fp8`
  (`Jackrong/Qwopus3.6-27B-v2-FP8` on L40S). `--keep`/`--stop` reuse one server across books.
  Boot budget, pull-stall detection and a translation watchdog (`CLOUD_LLM_MAX_HOURS`) bound the spend.
  Instance lifecycle comes from srt-skill's `vast_instance_lib.sh` / `runpod_pod_lib.sh`.
- `OmlxProvider(api_key=...)` and `--omlx-api-key` (env `OMLX_API_KEY`) on both CLIs: Bearer header for
  key-protected OpenAI-compatible endpoints; without a key the request shape is unchanged.
- Tests: `test/test_cloud_llm.py` (fake `vastai`, fake RunPod transport, local fake vLLM) covers
  no-key stop, readiness gate (200 + model id; 401 is not ready), boot-budget teardown, keep/stop,
  RunPod v2 payload (`args`, `ports`, `startSsh:false`), fp8 profile, unparsed-create adoption by label.
- Live run (2026-09-04, Vast.ai, Taiwan RTX 5090 at 0.356 USD/h): image pull + 18.7 GB model download +
  engine init took 19 min to `/v1/models`; the structural fixture (5 chapters) translated in 0.3 min;
  instance destroyed and confirmed absent. Two earlier attempts were aborted by bugs now fixed and
  tested: the shared Vast CLI wrapper appended `--raw` after `--args` (it became a vLLM argument and the
  container crash-looped while `actual_status` stayed `running`), and args-mode create responses are a
  Python-dict string, not JSON, so an unparsed-but-successful create leaked three instances before the
  adopt-by-label guard existed. RunPod path is unit-tested but not yet run live.

## [1.0.1] — 2026-08-10

### Fixed
- **Footnote pages and run-on front matter get bilingual ToC labels** (2026-08-10).
  Per-chapter footnote pages (`Superagency_FN001.xhtml`) open with the footnote text
  itself, so there is no title to extract and the ToC showed a sentence fragment in
  both columns. The label is now derived from the filename
  (`extract_epub._footnote_page_heading` -> "Footnote 3", paired with 註腳 3 in
  `nav_builder`). **Deliberately applied after `infer_role` and never fed into it**:
  classifying these pages as `notes` would flip `output_strategy` to `source_only`
  and silently stop translating twelve pages of real content — a test locks that
  down. Praise / "Also by" pages, whose first block runs the title straight into a
  blurb, are matched by prefix instead of exact string. Superagency now assembles
  with 0 English-only nav warnings, down from 31; no per-book repair script needed.
- **Chapter titles styled as `<p>` instead of `<h1>`-`<h6>` now reach the ToC**
  (2026-08-10). Publishers mark titles with CSS classes — `<p class="CN">CHAPTER 4</p>`
  + `<p class="CT">THE TRIUMPH…</p>` — which neither `extract_epub._first_heading`'s
  tag scan nor `build_nav_overrides`' heading check recognised. The English side of
  each nav label fell back to the first 80 characters of body text ("CHAPTER 1
  HUMANITY HAS ENTERED THE CHAT As 2022 drew to a close…") and the Chinese side was
  never generated, so every affected book needed a one-off repair script. This was
  the fifth book to hit it. New `extract_epub.styled_paragraph_title` and
  `offline_postprocess._leading_title_block_count` share one definition of a title
  block so both sides of a bilingual label agree; a chapter-number line and its
  title are joined into one label. Precedence is heading tag > canonical structural
  label > styled title: an earlier ordering let the styled title through first and
  turned "Contents" into "CONTENTS" and "Notes" into "NOTES: INTRODUCTION", breaking
  the zh lookup for pages that had previously worked.
  **ALL CAPS is the discriminator, and it is load-bearing.** Measured over the local
  corpus: 52 chapters use the styled-`<p>` shape and are all-caps, the 43 body-prose
  openings are long and mixed-case, and the 12 short mixed-case leading blocks are
  epigraphs and dedications ("My heart is not a home for cowards.") that must never
  become chapter titles. Structural pages that fall through (Copyright, Praise for …)
  are already covered by `STRUCTURAL_LABELS_ZH_TW`.
- **`collapse_acronym_glosses` learned the wrong Chinese rendering** (2026-08-10).
  Chinese has no word delimiters, so the greedy pre-bracket match on
  「隨著高度能動的人工智慧（AI）」 yielded 隨著高度能動的人工智慧, which matched nothing
  downstream: the full Superagency run collapsed 1 of 241 人工智慧 mentions. The unit
  tests missed it because their fixtures put the term at the start of a sentence,
  which real prose almost never does. Fixed by walking left from the bracket and
  stopping at a function word; the stop-character set is deliberately narrow, since
  用 (通用), 能 (智能), 有 (所有) and friends occur inside real terms — an over-wide
  first attempt trimmed 人工通用智慧 down to 智慧. After the fix: 人工智慧 241 -> 1,
  大型語言模型 60 -> 1.

## [1.0.0] — 2026-08-09

### Changed
- **Offline default model: `Qwen3.6-35B-Heretic-4bit` → `Qwopus3.6-27B-v2-MLX-4bit`**
  (2026-08-09). The 2026-06-25 promotion to the 35B rested on a ~5x throughput win
  and an automated register score from a single-chapter spot check. A full-chapter
  read-through of *Superagency* reversed it: the 35B's prose was judged clearly
  flatter with more 中國用語, and it needed 2 retries plus a single-paragraph
  fallback on the chapter the 27B translated with 0 retries and 0 dropped
  paragraphs. The 35B stays available as a ~4x-faster draft option via
  `--omlx-model`. A 470K-char book now takes ~3.2 h instead of ~0.8 h.

### Added
- **`OFFLINE_STYLE_RULES` in the offline prompt** (2026-08-09). The offline path
  previously shipped a bare format contract; the style rules had been stripped in
  the hy-mt2:7b era and never restored after the model got bigger. Without them the
  27B emits 47 mainland-Chinese usages per chapter (the 35B: 0) and both models
  produce 「A，這些A……」 anaphora and subject-predicate splits. The rules are
  structural only — relative-clause rewriting, no 「，這些X」 anaphora, 台灣 usage,
  inline 中譯（English）glosses, meaning preservation.
  **A sentence-LENGTH rule was tried and reverted**: 「單句超過 40 字就斷句」 scored
  best on the >55-char metric (29.2% → 10.0%) while collapsing sentence-length
  stdev 27.4 → 18.4 and crushing inline glosses 58 → 11. The reader rejected that
  output on sight. Optimising a length proxy optimises for monotony.
- **`offline_postprocess.collapse_acronym_glosses`** (2026-08-09). After the first
  「人工智慧（AI）」, later 人工智慧 become the bare acronym. Same cross-chunk cause as
  gloss dedupe — every chunk treats itself as the term's first mention, measured at
  人工智慧 x10 per chapter in two independent runs. The Chinese rendering is learned
  from the surviving gloss rather than hardcoded, so a book glossing 「人工智能（AI）」
  collapses that instead.
- **Per-book term table `<book_dir>/spec_terms.json`** (2026-08-09).
  `dispatch.load_fixed_terms()` reads `{"terms": {source: 中譯}}` and appends it to the
  offline system prompt as lookup data. Book-specific editorial data, deliberately not
  in this repo. Measured on Superagency ch.4: prompt 756 -> 1065 chars with 10 terms,
  still 0 retries / 0 dropped paragraphs, so the table does not reproduce the
  rule-count degradation that killed the length-rule version.
- **`offline_postprocess.dedupe_inline_glosses`** (2026-08-09). Keeps only the
  first 「中譯（English）」 per term book-wide. The gloss rule fires per chunk and the
  model has no cross-chunk memory, so terms get re-glossed repeatedly (104 glosses
  / 78 unique terms in one chapter). Deterministic dedupe, not a prompt fix.

### Fixed
- **Offline bilingual ToC: chapter titles in a `<header>` now get translated**
  (2026-06-25, systematic-debugging). Root cause: chapter titles live in
  `<header>` (chapter-number `<h1>` + `role="doc-subtitle"`), which
  `strip_non_content` drops, so `build_nav_overrides` saw the first body `<p>`
  (not a heading), skipped, and the EPUB ToC + in-body chapter titles rendered
  English-only. Only front/back matter (bare `<h1>`) got labels. New
  `offline_postprocess.translate_header_titles(book_dir, manifest, provider)`
  extracts the header title and batch-translates it through the model (marker-
  tagged, positional fallback); the driver calls it after `build_nav_overrides`.
- **translation_quality audit no longer false-fails on inline-bilingual ToC
  links.** A source_only paragraph rendered "English ｜ 中文" by
  `_bilingualize_contents_links` carries its translation inline; the audit now
  exempts any `src` paragraph that already contains Han (`_contains_han`) instead
  of demanding a separate `tgt` sibling or a `source_only.json` exception.
- Both verified end-to-end on *The Meaning of Your Life* (Arthur C. Brooks): 17
  chapters, 4/4 audit gates green, EPUB nav + in-body titles bilingual. New tests
  in `test/test_offline_header_titles.py` (6); full suite 324 passed.

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

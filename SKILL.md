---
name: book-translator
description: |-
  Translate full-length books (EPUB) to Traditional Chinese (Taiwan) with literary tone fidelity and cross-chapter coherence, preserving EPUB structure. Use when the user says "翻書", "翻電子書", "翻譯整本書", "book translate", "bilingual epub", or gives an .epub expecting literary translation. Main session extracts an OPF spine manifest plus a per-book glossary and style anchor, then dispatches parallel subagents (each gets glossary, style anchor, and last-paragraph carryover for coherence); the first item is previewed for tone confirmation before fanning out the rest. Produces a bilingual .epub (source + translation interleaved) with manifest/glossary/state for resume. Differs from translate-book (generic PDF/DOCX/EPUB, single-language, no coherence): book-translator targets literary works, uses Opus for the style-anchor pass and Sonnet for the parallel fan-out, and gates quality before shipping. Not for short articles (use polish) or SRT subtitles (use srt).
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Agent
  - AskUserQuestion
mutating: true
---

# Book Translator

Literary EPUB → bilingual EPUB translator. Optimized for novels and long-form non-fiction where tone fidelity and cross-chapter consistency matter.

## Triggers

Real user phrases that should route here:

- 「翻書」「翻電子書」「翻譯整本書」「文學翻譯」
- 「bilingual epub」「book translate」「translate this novel」
- 「把 XXX.epub 翻成繁中」「跑書」
- 用戶直接給一個 `.epub` 路徑並暗示需要文學翻譯

**Not for:**

- 短文 / 推文 / 一段話 → use `polish`
- SRT 字幕 → use `srt`
- PDF/DOCX 通用文檔翻譯（不需要 coherence + 單語輸出 OK）→ use `translate-book`
- 財報逐字稿 → use `forensic-transcript-translator` or `taiwan-earnings-translator`

## Differentiation from `translate-book`

| Dimension | translate-book | book-translator |
|-----------|---------------|-----------------|
| Input | PDF/DOCX/EPUB | EPUB only |
| Subagent model | Sonnet | **Sonnet 4.6** (default fan-out) / Opus 4.7 (ch.01 anchor + escalation) / local Ollama (opt-in) |
| Output | 翻譯版 (single language) | **Bilingual** (source + translation interleaved) |
| Coherence | None | Glossary + style anchor + carryover + spot-check |
| Quality gate | None | **Cross-modal eval** (Gemini 2.x Pro + GPT-4o, avg ≥ 8.0) |
| Resume | None | `state.json` |
| Domain | Generic | Literary (tone-critical) |

## Translation provider (engine)

The skill supports two providers:

- **Anthropic** (default for online quality-first) — Claude Code subagents
  (Opus 4.7 anchor + Sonnet 4.6 fan-out), parallel `concurrency=5`. Best
  quality; spends CC subscription quota. Triggered by the main workflow
  (see "Workflow" section below).
- Anthropic provider automatically uses the shared quota gate before Opus/Sonnet
  fan-out. The user does not need to say `agent-orch`:
  `/Users/fredchu/bin/agent-orch quota check --provider claude --threshold 85 --on-error fail-open --json`.
  If the command returns exit 2 or `decision=="wait"`, call `ScheduleWakeup`
  for JSON `resume_at`, persist the current `{book}_state.json`, and resume
  from completed chapter outputs after waking. If `decision=="probe_failed"`,
  retry after `retry_at`/`retry_after_seconds`; if `extra_usage.state` is
  `disabled`/`exhausted`, reduce fan-out concurrency or switch to offline
  provider instead of waiting for reset.
- **omlx** (**default for offline**) — local omlx server at `localhost:8090`,
  **sequential** MLX inference on Apple Silicon Metal.
  **Default model: `Qwopus3.6-27B-v2-MLX-4bit`** (Jackrong, Claude Opus 4.6/4.7
  TraceInversion distilled on Qwen3.5-27B; 14 GB, ~12 t/s, ~0.84 min per
  3000-char chunk). Quality-first: on a full-chapter human read-through it was
  judged clearly better than the 35B on prose rhythm and 台灣 usage, and it ran
  the chapter with **0 retries / 0 dropped paragraphs** where the 35B needed
  2 retries plus a single-paragraph fallback. A 470K-char book takes ~3.2 h.
  **`Qwen3.6-35B-Heretic-4bit`** (Qwen3.6-35B-A3B, 3B-active MoE; 20 GB,
  ~43 t/s) is the **speed alternate** at ~4x faster (~0.8 h for the same book)
  — use it for drafts via `--omlx-model Qwen3.6-35B-Heretic-4bit`.
  `enable_thinking:False` is required and OmlxProvider sets it for both.
  Engine auto-selected when `--engine` omitted.

  > **Default history — read before "optimising" this again.** 2026-06-25 promoted
  > the 35B on a ~5x throughput win plus a machine register score from a
  > single-chapter spot check. 2026-08-09 reverted it: a reader compared full
  > chapters and called the 35B's output clearly worse. Throughput and automated
  > register scores did not predict what a native reader caught in minutes.
  > Do not re-promote on benchmark numbers alone.

  See `2026-06-25-heretic-35b-a3b-local-translation-speedup.md` (the spot check
  that oversold the 35B) and `2026-05-23-local-translation-acceleration-research.md`.
- **Ollama** (alternate offline) — local Ollama server at `localhost:11434`,
  sequential (single GPU via llama.cpp). Useful for GGUF-only models (Hy-MT2,
  translategemma) when omlx is unavailable. Alternates: `translategemma:12b`
  (~1h27m E2E, fast but Simp leak high before TRADITIONAL_CHINESE_ENFORCEMENT
  prompt); `hy-mt2:7b` (~2h27m E2E, recursive split on 2-3 large chapters).
  See `2026-05-23-five-way-quality-comparison.md` for the full matrix that
  motivated the move off Ollama defaults.

### Offline mode — triggers + workflow

Triggers (main session routes here when the user says):

- 「離線翻 X.epub」「local translate X.epub」「translate offline X.epub」
  → **omlx + Qwopus3.6-27B-v2-MLX-4bit (default, quality-first)**
- 「快一點」「先出草稿」「draft」 → `--omlx-model Qwen3.6-35B-Heretic-4bit` (~4x faster)
- 「用 omlx 翻 X.epub」 → same default path
- 「用 hy-mt2:7b 翻書 X.epub」「用 translategemma:27b 翻書 X.epub」「ollama 翻書 X.epub」
  → ollama path with specified model (alternate / legacy)
- 「批次翻 X.epub Y.epub」「翻這幾本 ...」 → multi-book driver 同個 path
- Any phrase that names an ollama model + a book path → ollama path

Driver — main session runs (omlx default, no engine/model flag needed):

```bash
python3 ~/.claude/skills/book-translator/scripts/translate_book_ollama.py \
    --book /path/to/X.epub \
    --out /path/to/translations/
# expands to: --engine omlx --omlx-model Qwopus3.6-27B-v2-MLX-4bit
```

**Multi-book batch (2026-05-23 ship)** — 一次傳多本，driver 內序列翻譯，
per-book error containment：

```bash
python3 ~/.claude/skills/book-translator/scripts/translate_book_ollama.py \
    --book /path/A.epub /path/B.epub /path/C.epub \
    --out /path/to/translations/
```

Final summary 印每本 success/failed + duration。

For ollama path (alternate):

```bash
python3 ~/.claude/skills/book-translator/scripts/translate_book_ollama.py \
    --book /path/to/X.epub \
    --engine ollama --ollama-model hy-mt2:7b \
    --out /path/to/translations/
```

The driver runs end-to-end: extract → sequential per-chapter translate (with
Phase 1 `[[PARA_N]]` marker enforcement + 1 retry on misalignment) → offline
post-processing → assemble → 4 audit gates. Progress + state.json checkpointing
is per-chapter, so a killed/interrupted run resumes on the next invocation.

Offline post-processing (`scripts/offline_postprocess.py`) recovers the quality
the skipped glossary/nav build would have provided, since the offline path has no
glossary:
- **Simplified→Traditional** — every chapter is run through opencc `s2twp` at
  write time, fixing the local model's residual Simplified leak and normalising
  to Taiwan character forms (soft dependency: a no-op with one warning if opencc
  is absent).
- **Character-name coherence** — without a glossary the model drifts between
  transliteration variants (瑪德琳 vs 梅德琳); a conservative pass merges minority
  variants (length ≥ 3, free-standing, dominated ≥ 4×, never two real names) into
  the dominant form before assembly.
- **Acronym collapse** — after the first 「人工智慧（AI）」, later 人工智慧 become bare
  `AI`. Same cross-chunk cause as gloss dedupe: every chunk thinks it is the term's
  first mention (measured: 人工智慧 x10 in one chapter, in two independent runs). The
  Chinese rendering is learned from the surviving gloss, so a book that writes
  「人工智能（AI）」 collapses that instead. Runs after gloss dedupe, which leaves
  exactly one gloss to learn from.
- **Inline-gloss dedupe** — `OFFLINE_STYLE_RULES` asks for 「中譯（English）」 on a
  term's first mention, but the model sees one chunk at a time and has no memory
  of earlier chunks, so it re-glosses the same term in every chunk that mentions
  it (measured: 104 glosses for 78 unique terms in one chapter). A book-wide pass
  keeps the first occurrence and strips the rest. Do not try to fix this in the
  prompt — cross-chunk state is not something the model has.
- **Bilingual ToC** — nav_overrides are populated from each heading-led chapter's
  translated title so the table of contents renders bilingual.

The offline prompt also carries `dispatch.OFFLINE_STYLE_RULES` (structural rules
only: relative-clause rewriting, no 「，這些X」 anaphora, 台灣 usage, inline glosses,
meaning preservation). **Never add a sentence-length rule there** — see the
comment block above that constant for the measurements showing why a 「超過 40 字
就斷句」 rule wrecked the prose while scoring best on the length metric.

Expected runtime for a 23-chapter / ~150K-char book on M1 Max 32GB
(measured 2026-05-23 on *The Next Renaissance*):

| Engine + Model | E2E duration | Marker align | Simp leak | Quality (5-dim) |
|---|---|---|---|---|
| **omlx + Qwopus3.6-27B-v2-MLX-4bit (default, 2026-08-09)** | 2h13m | 23/23 = 100% | 0.9% | **★★★★★ preferred on human read-through** |
| omlx + Qwen3.6-35B-Heretic-4bit (speed alternate) | ~25-30min (est, ~5x) | 100% | 0.3% | ★★★☆☆ flatter prose, more 中國用語 |
| ollama + translategemma:12b | 1h27m | high | needs new prompt enforcement | ★★★☆☆ |
| ollama + hy-mt2:7b | 2h27m | moderate | ~100% Trad | ★★★☆☆ |
| ollama + translategemma:27b | 3-4h | similar to 12b | similar | ★★★☆☆ (12b 全面超越) |

> **The 5-dim "Opus tier" score was wrong about the 35B.** It came from a
> model-fit-scout spot check (ch9 of *The Next Renaissance*, 123 paras): Heretic-35B
> **43.2 t/s** vs Qwopus-27B-v2 **8.0 t/s** (5.4x), both 100% marker-aligned, scored
> at register parity. On a full chapter of *Superagency* with a reader comparing
> side by side, the 35B was rejected: flatter rhythm and noticeably more 中國用語.
> Measured on that chapter (106 paras, rules held constant): the 27B produced 45
> inline 中譯（English）glosses and 47 mainland usages without style rules, versus
> the 35B baseline's 58 glosses and 0 mainland usages — but with the rules applied
> the 27B lands at 104 glosses / 2 mainland usages and reads better. Both models
> need the style rules; only the 27B rewards them.
>
> Lesson for whoever tunes this next: a single-chapter automated register score
> plus a throughput number is not enough to move the default. Have someone read
> a full chapter of both.

Quality positioning:
- omlx + Qwopus3.6 ≈ matches Opus 4.7 register tier (sample diffs in chunk-size
  punch only, see _outputs research file)
- ollama + hy-mt2:7b / translategemma:12b: 0.7-1.0 lower on 5-dim eval; usable
  for drafts / offline reading, not interchangeable for literary fiction.

Single-chapter spot check (omlx default; no assemble, no audit):

```bash
python3 ~/.claude/skills/book-translator/scripts/translate_chapter_cli.py \
    --book /path/to/X.epub --chapter N \
    --out runs/spot-check/ --validate-markers
# expands to: --engine omlx --omlx-model Qwopus3.6-27B-v2-MLX-4bit
```

Cross-model benchmark (multiple models, side-by-side first paragraphs):

```bash
python3 ~/.claude/skills/book-translator/scripts/run_benchmark.py \
    --book /path/to/X.epub \
    --models hy-mt2:7b,translategemma:27b \
    --chapters 5,6 \
    --out runs/benchmark/
```

### Installing omlx + Qwopus3.6 (new default offline path)

omlx is the multi-model MLX inference server for Apple Silicon. Install via
Homebrew tap (one-time):

```bash
brew install jundot/omlx/omlx
brew services start jundot/omlx/omlx
# verify: curl http://127.0.0.1:8090/v1/models | jq '.data[].id'
```

Download the default Qwopus3.6-27B-v2-MLX-4bit (~14 GB) into `~/.omlx/models/`; the
~20 GB Qwen3.6-35B-Heretic-4bit speed alternate is optional
(use `HF_HUB_DISABLE_XET=1` — the xet transfer path stalls mid-download on large
MLX repos):

```bash
HF_HUB_DISABLE_XET=1 hf download froggeric/Qwen3.6-35B-A3B-Uncensored-Heretic-MLX-4bit \
    --local-dir ~/.omlx/models/Qwen3.6-35B-Heretic-4bit
# omlx auto-scans on next request; or POST /admin/api/models/Qwen3.6-35B-Heretic-4bit/load
```

Fallback (smaller RAM footprint, ~14 GB):

```bash
hf download Jackrong/Qwopus3.6-27B-v2-MLX-4bit \
    --local-dir ~/.omlx/models/Qwopus3.6-27B-v2-MLX-4bit
```

`chat_template_kwargs.enable_thinking=false` is mandatory for translation; the
`OmlxProvider` sets this by default, and it works on both the Qwopus default
(via its fixed chat template) and the Qwopus fallback. Manual API call:

```bash
curl -X POST http://127.0.0.1:8090/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{
        "model": "Qwopus3.6-27B-v2-MLX-4bit",
        "messages": [{"role":"user","content":"翻譯：Hello"}],
        "chat_template_kwargs": {"enable_thinking": false}
    }'
```

Acceleration toggles (`dflash_enabled` / `mtp_enabled` / `specprefill_enabled`)
do not help on M1 Max + 27B + translation — verified 2026-05-23 across 12 paths
(see `wiki/_outputs/2026-05-23-local-translation-acceleration-research.md`).
Leave them all off for production.

### Installing the Hy-MT2 models (alternate offline path)

Hy-MT2 GGUFs have a broken auto-generated chat template on ollama 0.24. Install
via the project's pre-built Modelfiles (one-time):

```bash
ollama pull hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M
ollama pull hf.co/tencent/Hy-MT2-7B-GGUF:Q4_K_M
ollama create hy-mt2:1.8b -f ~/.claude/skills/book-translator/scripts/modelfiles/Modelfile.hy-mt2-1.8b
ollama create hy-mt2:7b   -f ~/.claude/skills/book-translator/scripts/modelfiles/Modelfile.hy-mt2-7b
```

translategemma models work as-is: `ollama pull translategemma:{4b,12b,27b}`.

Provider abstraction lives in `scripts/providers/`; new engines plug in by
subclassing `TranslationProvider` and registering with `provider_factory()`.

## Contract

Given an EPUB and target language (default: 台灣繁體中文), this skill:

1. Reads the entire book, extracts a glossary (characters / places / terms / chapter titles / style anchor).
2. Auto-populates per-book nav overrides from glossary chapter titles.
3. Translates chapter 1 in the main session as the **style sample**.
4. Asks the user to confirm tone / edit glossary before fan-out.
5. Dispatches parallel subagents (Sonnet 4.6 default, concurrency 5; Opus 4.7 on validation escalation) to translate remaining `translate` spine items — each receives glossary + style sample + last-paragraph carryover.
6. Assembles a full-fidelity bilingual EPUB from the full OPF spine: original XHTML paths, OPF idrefs, CSS, fonts, images, class names, and internal href targets are preserved; Traditional Chinese paragraphs are inserted after English text blocks.
7. Runs structural QA (`structural_audit.py`), bilingual coverage QA (`bilingual_coverage_audit.py`), href resolution QA (`href_resolve_audit.py`), translation placeholder/length QA (`translation_quality_audit.py`), and a separate translation spot-check pass (random 5 paragraphs + character name audit) before shipping.
8. Crash-safe: every step persists to `{book}_state.json`; re-running resumes from last completed chapter.

## Architecture

```
Main session (Claude Code, Opus 4.7 for ch.01 anchor)
├── 1. extract_epub.py book.epub → full OPF spine manifest v2   (deterministic)
├── 2. glossary build (LLM call in main session)               (latent)
│      └── reads full book → glossary.json
├── 2.5. populate translations_extra nav_overrides             (deterministic)
├── 3. translate ch.01 in main session → style_sample          (latent)
├── 4. INTERACTIVE PREVIEW GATE                                (user-in-loop)
│      └── print ch.01 preview → user [confirm | edit glossary | edit style]
├── 5. Agent dispatch ch.02 .. ch.N (concurrency 5)            (parallel subagents, Sonnet 4.6 default)
│      └── each gets: chapter + glossary + style_sample + carryover
├── 6. assemble.py → bilingual.epub                            (deterministic)
├── 7. structural_audit.py → spine/image/state completeness     (deterministic)
└── 8. spot-check (random 5 paragraphs + name audit)           (latent)
```

### Shared deterministic modules

Cross-cutting deterministic logic lives in named modules; all read/write
call sites delegate so behaviour stays consistent across extract,
dispatch, assemble, and audits.

**Content / EPUB I/O:**
- **`scripts/content_blocks.py`** — canonical "what counts as a paragraph".
  `TEXT_TAGS`, `BLOCK_TAGS`, `walk_text_nodes`, `extract_blocks`,
  `extract_paragraphs`, `strip_non_content`, `TextBlock`/`ImageBlock`.
  Used by `dispatch.html_to_paragraphs` / `dispatch.html_to_blocks` (thin
  wrappers), `assemble._text_nodes_for_bilingual`,
  `bilingual_coverage_audit`. `translation_quality_audit` audits already-
  marked `class="src"` pairs, not raw paragraphs — intentionally outside.
- **`scripts/epub_reader.py`** — EPUB-zip reader (context manager).
  `EPUBReader`, `OPFPackage`, `ManifestItem`, `find_opf_path`.
  Tolerant of missing `container.xml` AND missing `.opf` (returns `None`).
  Used by `extract_epub.py`, `bilingual_coverage_audit.py`,
  `translation_quality_audit.py`.
- **`scripts/manifest.py`** — manifest v2 normalization + persistence.
  `SpineEntry` dataclass, `normalize_entries(manifest)`, `chapters_from_spine`,
  `entry_original_path`, `load`, `save`. Consolidates v2 spine reading +
  legacy `chapters[]` backfill that was previously duplicated in
  extract / assemble / structural_audit / state.

**Persistence:**
- **`scripts/translations_extra.py`** — `translations_extra.json` owner.
  `TRANSLATIONS_EXTRA_FILENAME`, `load`, `save`, `write_nav_overrides`.
  `glossary.write_translations_extra_nav_overrides` is now a back-compat
  alias.
- **`scripts/glossary.py`** gains `load_glossary` + the moved register
  lookup `REGISTER_HINTS_PATH` / `resolve_register_override` /
  `resolve_register_rules` (previously in dispatch.py).
- **`scripts/state.py`** — `ChapterEntry` dataclass owns the
  `output_strategy` mutation invariant. The four mark_* helpers
  (`mark_done` / `mark_failed` / `mark_source_ready` / `mark_dropped`)
  delegate so the invariant is preserved uniformly. State.json on disk
  shape unchanged.

**Assemble pipeline (split from the former 770-line god module):**
- **`scripts/assemble.py`** — thin orchestrator. `assemble(book_dir, out_path)`
  sequences manifest load → spine entries → preflight → per-entry bilingual
  rewrite → nav build → archive write.
- **`scripts/bilingual_rewriter.py`** — `insert_bilingual(src_html, entry,
  translations)` does per-paragraph English/中文 interleaving.
  `STRUCTURAL_LABELS_ZH_TW` lives here.
- **`scripts/nav_builder.py`** — `build_nav_xhtml`, `nav_path`,
  `patch_toc_ncx`, `missing_nav_zh_warnings`, `source_ncx_path`. Generates
  the bilingual nav and patches NCX for legacy readers.
- **`scripts/archive_writer.py`** — `write_from_source_archive` (default,
  overlays rewritten files onto source EPUB) and `write_standalone_archive`
  (fallback when source archive isn't accessible).
- **`scripts/opf_builder.py`** — `build_minimal_opf` + `fallback_opf_path`
  for the standalone path.

**Audit interface:**
- **`scripts/audit_result.py`** — canonical `AuditResult(name, status,
  failures, details)` dataclass + `.passed` / `.format_lines()`. Replaces
  the divergent shapes that the 4 audits previously returned.
- **`scripts/audit_suite.py`** — `run_all(source, output, book_dir)`,
  `all_passed`, `format_summary`. CLI: `python3 -m scripts.audit_suite ...`
  or `python3 scripts/audit_suite.py ...`.
- Each audit module retains its legacy `audit(...) -> tuple[bool, list[str]]`
  for back-compat, plus a `run(...) -> AuditResult` that audit_suite calls.

### 4 Coherence Mechanisms

1. **Glossary injection** — main session reads full book → JSON of characters / places / terms; every subagent prompt receives it.
2. **Style anchor** — chapter 1 translated in main session becomes the reference style; every subagent prompt includes the first 500 chars as anchor.
3. **Last-paragraph carryover** — each subagent gets the last 200 chars of the *previous chapter's translation* so the opening flows.
4. **Spot-check pass** — after all chapters complete, main session samples 5 random paragraphs and cross-references character name appearances against glossary. Mismatches → flag, do not auto-fix (avoid silent corruption).

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

## Workflow

### Step 1: Collect parameters

- `book_path` — required, must be `.epub`
- `target_lang` — default `zh-tw` (台灣繁體中文; use 台灣用語 not 大陸用語)
- `style_hint` — optional user override of style anchor
- `concurrency` — default 5; max 10 (CC quota safety)
- `out_dir` — default same dir as input

### Step 2: Extract full OPF spine

```bash
python3 {baseDir}/scripts/extract_epub.py "<book_path>" --out "<out_dir>"
```

`extract_epub.py` delegates `META-INF/container.xml` lookup + OPF parsing to
`scripts/epub_reader.py` (shared with the audit scripts) and manifest v2
spine normalization to `scripts/manifest.py` (`SpineEntry` +
`normalize_entries` + `chapters_from_spine`). The rest of the extraction
pipeline (asset copy, XHTML mirror, manifest.json write) stays in
`extract_epub.py`.

Produces `<out_dir>/<book_stem>/source.opf`, verbatim asset directories
(`css/`, `fonts/`, `images/`), original XHTML copies under `xhtml/`,
compatibility files in `chapters/item_NNN.html`, and `manifest.json`.
The manifest source of truth is `spine[]`, not `chapters[]`. Every original OPF
spine item is represented unless it is explicitly `drop_explicit` with a
non-empty reason. The compatibility `chapters[]` list contains only
`translate` items for current dispatch tooling.
Each spine entry records the original EPUB package path (`original_path`) and
original OPF idref (`original_idref`) so assembly can emit the source layout
instead of synthetic chapter filenames.

Output strategies:

- `translate` — require `item_NNN_translation.txt`; assemble bilingual source /
  translation interleaving.
- `source_only` — no translation file required; preserve source content in the
  output spine.
- `nav_generated` — omit source nav and regenerate EPUB nav/NCX from represented
  output spine.
- `drop_explicit` — omit only with a visible, non-empty reason.

Default policy: body prose, epilogue, and English structural prose use
translation files. Structural pages may be kept `source_only` only when they
are genuinely image-only or deliberately listed in
`translations/source_only.json`; the assembler no longer invents placeholder
translations for missing paragraphs. The source nav is replaced with a
deterministic bilingual nav at the original nav path.

### Step 3: Build glossary

Main session reads the full book content (concatenate chapter texts), then makes one inline LLM call to extract characters / places / terms / style_anchor. Result written to `<out_dir>/<book_stem>/glossary.json`.

Schema:

```json
{
  "characters": {"Napoleon": "拿破崙", "Snowball": "雪球"},
  "places": {"Animal Farm": "動物農莊"},
  "terms": {"Beasts of England": "英格蘭的野獸"},
  "chapter_titles_zh": {"CHAPTER 1": "第一章"},
  "style_anchor": {
    "register": "literary plain prose",
    "avoid": ["四字結構過多", "翻譯腔", "過度書面化"],
    "prefer": ["口語節奏", "略諷刺", "短句"]
  }
}
```

### Step 2.5: Auto-populate nav overrides (MANDATORY — every spine item)

After glossary build, the main session calls
`translations_extra.write_nav_overrides(glossary, manifest, book_dir)` to
populate `<book_dir>/translations_extra.json::nav_overrides` from the
glossary's `chapter_titles_zh` field.
(`glossary.write_translations_extra_nav_overrides` is a back-compat alias.)
This drives both nav rendering AND bilingual chapter title display in the
assembled EPUB. If `translations_extra.json` already exists, existing keys
are preserved (user overrides are not clobbered).

**Contract — every spine item must have a Chinese nav label.**

`assemble.py` runs in `strict_nav=True` mode by default and **aborts with
`ValueError`** if any spine item's nav label would render English-only after
applying nav_overrides + glossary chapter_titles_zh + the built-in
`STRUCTURAL_LABELS_ZH_TW` (covers `Cover`/`Title Page`/`Copyright`/
`Dedication`/`Acknowledgments`/`Notes`/`Index`/`About the Author` etc.).
This covers three categories explicitly:

1. **Body chapters** — pulled from `chapter_titles_zh` automatically; if the
   glossary missed one, the main session must add it before assembly.
2. **Image/plate pages** (book.opf often lists 10-30 plate inserts whose source
   labels are long photo captions) — they have no canonical glossary entry, so
   the main session must write a short Chinese label per plate
   (e.g. `"圖版：亞瑟·洛克"`) into `nav_overrides` keyed by the source idref.
3. **Front-/back-matter structural pages** (Foreword, Preface, Epigraph,
   Appendix, Timeline) — fall back to `STRUCTURAL_LABELS_ZH_TW` when the title
   matches; otherwise add to `nav_overrides`.

If the assembler aborts, the error message lists every missing idref + the
fallback English label it saw — fix `translations_extra.json` and re-run. The
`strict_nav=False` escape hatch exists only for low-level fixtures inside
the skill's own test suite; never call assembly with it from production
workflows.

### Step 4: Translate ch.01 inline (style sample)

Main session translates chapter 1 directly (not via subagent) so the resulting translation becomes the canonical style anchor.

### Step 5: Interactive preview gate

Use `AskUserQuestion` tool to present:

```
=== Chapter 1 preview ===
[ORIGINAL] <first 300 chars>
[TRANSLATION] <first 300 chars of zh-tw>

=== Glossary ===
<json dump, top 10 entries>
```

Options: `[c]onfirm and continue` / `[g]lossary edit` / `[s]tyle edit` / `[r]e-translate ch.1`.

On `[c]`: set `state.style_confirmed = true`, proceed. On `[g]`/`[s]`: accept user input, update glossary.json, retranslate ch.1. On `[r]`: regenerate ch.01 with current glossary/style.

**Bypass condition**: if `state.style_confirmed == true` (resume case), skip the gate.

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

### Step 7: Assemble bilingual EPUB

```bash
python3 {baseDir}/scripts/assemble.py \
  --book "<out_dir>/<book_stem>/" \
  --out "<book_stem>_bilingual.epub"
```

`assemble.py` is a thin orchestrator that sequences four sibling modules:
- `bilingual_rewriter.insert_bilingual` does per-paragraph English/中文 interleaving
- `nav_builder.build_nav_xhtml` + `patch_toc_ncx` generate the bilingual nav
- `archive_writer.write_from_source_archive` (default) or
  `write_standalone_archive` (fallback) writes the output EPUB
- `opf_builder.build_minimal_opf` is used only by the standalone path

Per-page structure: original English text nodes keep their source classes and
also receive `src`; inserted Traditional Chinese sibling paragraphs receive
`tgt tgt-zh` plus the inherited source classes. Original relative paths and
internal hrefs are not rewritten.

**Auto-bundled translations payload.** `assemble.py` writes two things into
`<opf_dir>/translations/` inside the output EPUB so audits can read them:

1. **Every `*.json` under `<book_dir>/translations/`** — user-authored payloads
   (custom `source_only.json` lists, `register_hints.json`, etc.) are copied
   verbatim into `OEBPS/translations/`.
2. **Auto-generated `source_only.json`** — if the user has not authored one,
   assembly scans every rewritten `source_only` spine item, collects the
   `class="src"` paragraphs that have no zh sibling, and writes their text
   into `source_only.json`. This satisfies both
   `translation_quality_audit.py` and `bilingual_coverage_audit.py` (both
   load the same file from the EPUB to whitelist intentionally-English
   paragraphs like endnote citations).

Assembly fails closed: any `translate` item missing its `item_NNN_translation.txt`
is a hard error. Source-only pages are emitted without translation, and explicit
drops require a reason.

### Step 8: Audit gates

All 4 deterministic audits run together via `audit_suite`:

```bash
python3 {baseDir}/scripts/audit_suite.py \
  --source "<book_path>" \
  --output "<book_stem>_bilingual.epub" \
  --book-dir "<out_dir>/<book_stem>"
```

`audit_suite.run_all(...)` returns `list[AuditResult]`; the orchestrator can
aggregate via `all_passed(results)` and report via `format_summary(results)`.
Each `AuditResult(name, status, failures, details)` is the canonical shape —
the audits previously returned three different shapes, now unified.

Individual audits remain runnable standalone if you want one gate at a time:

```bash
python3 {baseDir}/scripts/structural_audit.py \
  --source "<book_path>" \
  --output "<book_stem>_bilingual.epub" \
  --book-dir "<out_dir>/<book_stem>"

python3 {baseDir}/scripts/bilingual_coverage_audit.py \
  --source "<book_path>" \
  --output "<book_stem>_bilingual.epub"

python3 {baseDir}/scripts/href_resolve_audit.py \
  --output "<book_stem>_bilingual.epub"

python3 {baseDir}/scripts/translation_quality_audit.py \
  --output "<book_stem>_bilingual.epub"
```

`structural_audit.py` checks source-vs-output spine representation, missing
translations for translate items, state schema validity, cover page presence,
source-only image preservation, and at least one body chapter.

`bilingual_coverage_audit.py` and `translation_quality_audit.py` share OPF
lookup + spine walking through `scripts/epub_reader.py` (context manager);
`bilingual_coverage_audit.py` additionally shares the paragraph-walking
algorithm through `scripts/content_blocks.py`. A missing or malformed
EPUB package is reported as an audit failure rather than raising — this is
the canonical tolerant behavior.

`bilingual_coverage_audit.py` fails when a long English content paragraph lacks
an adjacent Han-character sibling, **except** when the paragraph's text is
listed in `OEBPS/translations/source_only.json` inside the EPUB (same exception
contract as `translation_quality_audit.py`). `href_resolve_audit.py` fails when
any internal XHTML link target is missing from the EPUB zip.
`translation_quality_audit.py` fails on Round-2-style placeholders,
span-concatenated headings, too-short target paragraphs, or unlisted
source-only paragraphs.

### Step 9: Translation spot-check

Main session samples 5 random paragraphs across chapters:

- Character names match glossary entries
- No obvious omission (translation paragraph count vs. source)
- Tone matches style anchor

Report findings; do not auto-fix.

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

## State machine (`state.json`)

```json
{
  "book": "animal_farm.epub",
  "started": "2026-05-14T03:00:00Z",
  "target_lang": "zh-tw",
  "glossary_built": true,
  "style_confirmed": false,
  "chapters": {
    "item_001": {"output_strategy": "source_only", "status": "source_ready"},
    "item_002": {"output_strategy": "translate", "status": "done", "translation_hash": "abc123", "carryover": "...最後 200 字..."},
    "item_003": {"output_strategy": "drop_explicit", "status": "dropped", "reason": "promotional page omitted"}
  }
}
```

Allowed `output_strategy`: `translate`, `source_only`, `nav_generated`,
`drop_explicit`. Allowed `status`: `pending`, `in_progress`, `done`, `failed`,
`source_ready`, `dropped`. The unstructured status `skipped` is invalid.

Resume: re-running on the same book reads `state.json` → skip `done` and
`source_ready` → retry `failed` once → process `pending`.

**Mutation invariant**: `output_strategy` is the spine item's identity and
is preserved across status mutations. `scripts/state.py::ChapterEntry`
owns this invariant — the module-level `mark_done` / `mark_failed` /
`mark_source_ready` helpers all delegate to a single dataclass that
guarantees `output_strategy` is never silently dropped. `mark_dropped`
is the explicit exception (it replaces `output_strategy` with
`drop_explicit`).

## Phase 3 — Cross-modal eval gate (deferred to first real translation)

The quality gate evaluates a real Opus-4.7 translation output, not the SKILL.md
scaffold. Running it on a synthetic / scaffold-stage sample tests the wrong
thing.

When this skill is **invoked from within a Claude Code main session** (the typical
case), the main-session LLM runs the eval through the **`Agent` tool**, NOT the
Python adapter / `claude --print` subprocess. CC has nesting detection that
blocks recursive `claude` CLI calls, so the subprocess path will hang.

**In-CC flow** (use this when the user types `/skillify book-translator` or
`run cross-modal eval` from inside a CC session):

1. Pick a representative chapter (default fixture: Animal Farm Ch.I → `ch_03`).
2. After translating, save the translated text to `<out_dir>/<book>/chapters/ch_NN_translation.txt`.
3. Main session builds the eval prompt via
   `scripts.slots.base.build_eval_prompt(skill_text, task_description)`.
4. Main session spawns **two `Agent` calls in parallel** (single message,
   multiple tool uses):
   - **Slot A**: `subagent_type=general-purpose`, `model=opus`, prompt = the eval prompt.
   - **Slot B**: `subagent_type=general-purpose` with a Bash call invoking
     `python3 ~/.claude/skills/codex-dispatch/scripts/codex_dispatch_role.py`
     (MODE=verifier, task = the eval prompt) — uses GPT-5 / GPT-4.1 via OpenAI
     subscription, **does not consume CC quota**.
5. Parse both JSON replies with `scripts.slots.base.parse_score_json`,
   aggregate with `scripts.aggregator.aggregate`, persist via
   `scripts.receipt.write_receipt`.

**From a plain shell** (non-CC) flow:

```bash
python3 -m scripts.cross_modal_eval \
  --task "Translate Chapter 1 of Animal Farm to 台灣繁體中文 with plain literary register" \
  --output <out_dir>/animal_farm/chapters/ch_03_translation.txt
```

That CLI uses `ClaudeCodeSlotA.score()` which shells out to `claude --print` —
fine outside CC, hangs inside CC.

Pass criteria (both flows): avg ≥ 8.0 across 5 dimensions, no model scores any
dimension < 5. If fail, iterate `SUBAGENT_PROMPT_TEMPLATE` in
`scripts/dispatch.py`.

The deterministic pipeline (full-spine extract → assemble → structural audit)
is tested separately from LLM translation quality. Those deterministic tests do
not need an LLM.

## Translation style discipline

> Source: Bocky 學長 2026-05-14 實戰驗證 — Opus 4.7 + 這幾條 vs 不加，品質顯著
> 提升。已 baked into `SUBAGENT_PROMPT_TEMPLATE` (rules 5-8) and the default
> `style_anchor` hints in `GLOSSARY_PROMPT`.

### Layout invariants (handled by `assemble.py`)

- **每個英文段落後緊接該段中譯**，per-paragraph interleave (not chapter-level).
- **詩 / 引言 / 列表 / blockquote / preformatted code / definition lists** 全部
  比照處理 — `dispatch.html_to_paragraphs` / `dispatch.html_to_blocks`
  (thin wrappers over `scripts/content_blocks.py` `extract_paragraphs` /
  `extract_blocks`) 抓 `<p>, <h1-h6>, <blockquote>, <li>, <pre>, <dt>, <dd>`
  全納入翻譯，images 透過 `BLOCK_TAGS` 維持位置；同一份 `TEXT_TAGS` 也是
  `assemble._text_nodes_for_bilingual` 和 `bilingual_coverage_audit` 的單一
  source of truth。漏網的內容（極少數）會在 spot-check 抓到。

### Translation rules (baked into subagent prompt rules 5-8)

1. **專有名詞 / 縮寫 / 技術詞首次出現用英中並列**：
   - First-in-chapter: 「大型語言模型（LLM）」「人類回饋強化學習（RLHF）」「通用人工智慧（AGI）」
   - Subsequent: 中文 only.
2. **保留作者第一人稱**：`I asked AI...` → 「我問 AI⋯」NOT「筆者問 AI⋯」
   — narrative 親近感是商管科普 voice 的核心。
3. **例句 / AI 對話 / 打油詩保原作風格與幽默**：機智 / 反差 / 自嘲口吻必須留住；
   AI limerick 可重組押韻（中文押韻為主）不必逐字。
4. **整體 register 商管科普 narrative，不學術化**：短句 / 口語 / 具體例子 /
   白話優於成語堆疊。

### Why this is gated up-front

沒這幾條 default Opus 翻商管科普容易：(a) 把 `I` 翻成「筆者」/「作者」/「我們」失去
narrative；(b) 對話翻成「面試官 vs 應徵者」教科書腔；(c) 縮寫翻成中文後讀者
recall 不回原 term；(d) 整體 register 漂向論文體。Phase 3 cross-modal eval 是
最後守門員。

## Output

This skill writes to:

- `<out_dir>/<book_stem>/glossary.json` — extracted glossary (per book)
- `<out_dir>/<book_stem>/state.json` — resume state
- `<out_dir>/<book_stem>/spec_terms.json` — **optional, user-authored**: per-book
  `{"terms": {source: 中譯}}` table agreed before translation starts. Loaded by
  `dispatch.load_fixed_terms()` and appended to the offline system prompt as lookup
  data (not as another rule). Absent file = previous behaviour.
- `<out_dir>/<book_stem>/manifest.json` — full OPF spine manifest v2
- `<out_dir>/<book_stem>/chapters/item_NNN.html` — extracted source spine items
- `<out_dir>/<book_stem>/chapters/item_NNN_translation.txt` — per-item translation for `translate` items
- `<out_dir>/<book_stem>/translation_log/<chapter_id>.json` — per-chapter prompt, raw response, parsed translation, validation warnings, model, and marker counts
- `<out_dir>/<book_stem>/cover.jpg` (or `.png`) — extracted cover image, embedded into the output EPUB by `assemble.py` (3-strategy lookup: EPUB 3 `properties="cover-image"` → EPUB 2 `meta name="cover"` → id-contains-"cover" image)
- `<out_dir>/<book_stem>/images/` — every inline image from the source EPUB, flattened to bare filenames. `assemble.py` calls `html_to_blocks()` to interleave text-translation pairs with standalone image blocks (`<div><img/></div>` and `<figure>` wrappers); inline decorative imgs inside `<p>text<img/></p>` are dropped as visual markers
- `<out_dir>/<book_stem>_bilingual.epub` — final bilingual EPUB (the only artifact the user needs to keep)

All paths under user-provided `out_dir` (default: directory of input epub). The skill does NOT write to `~/.claude`, system cache, git, Apple Notes, or any other location.

## Fixtures

Two roles, kept distinct:

- **Deterministic test fixture** (pytest, `test/test_extract_epub.py` + `test/test_e2e.py`):
  `~/ghkb/interested/bilingual_book_maker/test_books/animal_farm.epub` — public-domain,
  ~50K input tokens, 10 body chapters. Stable, free to test against on every commit.
- **First-real-run target** is your choice — any book you legally own. Use the
  Phase 3 cross-modal eval to validate translation quality on the first chapter
  before fanning out to the rest of the book. The full pipeline (extract → glossary
  → style sample → subagent fan-out → assemble → 5 audit gates) is book-agnostic.

## Distribution

Disclaimer: this skill is intended for **public-domain works or books you
legally own**. Do not use it to translate copyrighted material you do not have
the right to reproduce. Per-book overrides (custom dedication / acknowledgments
paragraph translations, custom nav labels) live in
`<book_dir>/translations_extra.json` and `<book_dir>/spec_terms.json` — never in this repo.

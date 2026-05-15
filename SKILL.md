---
name: book-translator
description: |-
  Translate full-length books (EPUB) to Traditional Chinese (Taiwan) with literary tone fidelity, cross-chapter coherence, and deterministic EPUB structure preservation. Use when the user says "翻書", "翻電子書", "翻譯整本書", "book translate", "bilingual epub", or provides an .epub file expecting literary translation. Main session extracts a full OPF spine manifest v2 plus a per-book glossary + style anchor, then dispatches parallel subagents to translate translate-strategy spine items — each subagent receives glossary + style anchor + last-paragraph carryover for cross-chunk coherence. After the first translated item a preview is printed and the user confirms tone before fanning out the rest. Produces a bilingual .epub (source + translation interleaved), manifest.json, glossary.json, and state.json for resume. Differs from translate-book (which is a generic PDF/DOCX/EPUB translator using Sonnet, single-language output, no coherence mechanisms) — book-translator targets literary works specifically, uses Opus 4.7, gates translation quality with cross-modal eval, and gates EPUB completeness with structural_audit.py before shipping. Not for short articles (use polish), not for SRT subtitles (use srt).
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
| Subagent model | Sonnet | **Opus 4.7** |
| Output | 翻譯版 (single language) | **Bilingual** (source + translation interleaved) |
| Coherence | None | Glossary + style anchor + carryover + spot-check |
| Quality gate | None | **Cross-modal eval** (Gemini 2.x Pro + GPT-4o, avg ≥ 8.0) |
| Resume | None | `state.json` |
| Domain | Generic | Literary (tone-critical) |

## Contract

Given an EPUB and target language (default: 台灣繁體中文), this skill:

1. Reads the entire book, extracts a glossary (characters / places / terms / style anchor).
2. Translates chapter 1 in the main session as the **style sample**.
3. Asks the user to confirm tone / edit glossary before fan-out.
4. Dispatches parallel subagents (Opus 4.7, concurrency 5) to translate remaining `translate` spine items — each receives glossary + style sample + last-paragraph carryover.
5. Assembles a full-fidelity bilingual EPUB from the full OPF spine: original XHTML paths, OPF idrefs, CSS, fonts, images, class names, and internal href targets are preserved; Traditional Chinese paragraphs are inserted after English text blocks.
6. Runs structural QA (`structural_audit.py`), bilingual coverage QA (`bilingual_coverage_audit.py`), href resolution QA (`href_resolve_audit.py`), translation placeholder/length QA (`translation_quality_audit.py`), and a separate translation spot-check pass (random 5 paragraphs + character name audit) before shipping.
7. Crash-safe: every step persists to `{book}_state.json`; re-running resumes from last completed chapter.

## Architecture

```
Main session (Claude Code, Opus 4.7)
├── 1. extract_epub.py book.epub → full OPF spine manifest v2   (deterministic)
├── 2. glossary build (LLM call in main session)               (latent)
│      └── reads full book → glossary.json
├── 3. translate ch.01 in main session → style_sample          (latent)
├── 4. INTERACTIVE PREVIEW GATE                                (user-in-loop)
│      └── print ch.01 preview → user [confirm | edit glossary | edit style]
├── 5. Agent dispatch ch.02 .. ch.N (concurrency 5)            (parallel subagents)
│      └── each gets: chapter + glossary + style_sample + carryover
├── 6. assemble.py → bilingual.epub                            (deterministic)
├── 7. structural_audit.py → spine/image/state completeness     (deterministic)
└── 8. spot-check (random 5 paragraphs + name audit)           (latent)
```

### 4 Coherence Mechanisms

1. **Glossary injection** — main session reads full book → JSON of characters / places / terms; every subagent prompt receives it.
2. **Style anchor** — chapter 1 translated in main session becomes the reference style; every subagent prompt includes the first 500 chars as anchor.
3. **Last-paragraph carryover** — each subagent gets the last 200 chars of the *previous chapter's translation* so the opening flows.
4. **Spot-check pass** — after all chapters complete, main session samples 5 random paragraphs and cross-references character name appearances against glossary. Mismatches → flag, do not auto-fix (avoid silent corruption).

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
  "style_anchor": {
    "register": "literary plain prose",
    "avoid": ["四字結構過多", "翻譯腔", "過度書面化"],
    "prefer": ["口語節奏", "略諷刺", "短句"]
  }
}
```

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

Use the `Agent` tool with `model: "opus"`, batch size = concurrency. Each subagent task:

```
You are translating Chapter <N> of <book_title> from English to 台灣繁體中文.

GLOSSARY (mandatory — use these exact translations):
<glossary.json>

STYLE ANCHOR (match this register):
<first 500 chars of ch.01 translation>

CARRYOVER (last 200 chars of ch.<N-1> translation; your opening should flow from this):
<carryover>

CHAPTER <N> SOURCE:
<chapter html, stripped of nav/footer>

Return ONLY the translated text (no commentary). Preserve paragraph boundaries.
```

After each batch, update `state.json`: `chapters[N].status = "done"` + `translation_hash`. Failed chapters → 1 retry; second failure → mark `failed`, do not block subsequent batches.

### Step 7: Assemble bilingual EPUB

```bash
python3 {baseDir}/scripts/assemble.py \
  --book "<out_dir>/<book_stem>/" \
  --out "<book_stem>_bilingual.epub"
```

Per-page structure: original English text nodes keep their source classes and
also receive `src`; inserted Traditional Chinese sibling paragraphs receive
`tgt tgt-zh` plus the inherited source classes. Original relative paths and
internal hrefs are not rewritten.

Assembly fails closed: any `translate` item missing its `item_NNN_translation.txt`
is a hard error. Source-only pages are emitted without translation, and explicit
drops require a reason.

### Step 8: Structural QA

```bash
python3 {baseDir}/scripts/structural_audit.py \
  --source "<book_path>" \
  --output "<book_stem>_bilingual.epub" \
  --book-dir "<out_dir>/<book_stem>"
```

This deterministic gate is separate from translation quality eval. It checks
source-vs-output spine representation, missing translations for translate
items, state schema validity, cover page presence in the output spine,
source-only image preservation, and at least one body chapter.

Also run:

```bash
python3 {baseDir}/scripts/bilingual_coverage_audit.py \
  --source "<book_path>" \
  --output "<book_stem>_bilingual.epub"

python3 {baseDir}/scripts/href_resolve_audit.py \
  --output "<book_stem>_bilingual.epub"

python3 {baseDir}/scripts/translation_quality_audit.py \
  --output "<book_stem>_bilingual.epub"
```

`bilingual_coverage_audit.py` fails when a long English content paragraph lacks
an adjacent Han-character sibling. `href_resolve_audit.py` fails when any
internal XHTML link target is missing from the EPUB zip.
`translation_quality_audit.py` fails on Round-2-style placeholders,
span-concatenated headings, too-short target paragraphs, or unlisted
source-only paragraphs.

### Step 9: Translation spot-check

Main session samples 5 random paragraphs across chapters:

- Character names match glossary entries
- No obvious omission (translation paragraph count vs. source)
- Tone matches style anchor

Report findings; do not auto-fix.

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
  比照處理 — `html_to_paragraphs` 抓 `<p>, <h1-h6>, <blockquote>, <li>, <pre>,
  <dt>, <dd>` 都納入翻譯。漏網的內容（極少數）會在 spot-check 抓到。

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
- `<out_dir>/<book_stem>/manifest.json` — full OPF spine manifest v2
- `<out_dir>/<book_stem>/chapters/item_NNN.html` — extracted source spine items
- `<out_dir>/<book_stem>/chapters/item_NNN_translation.txt` — per-item translation for `translate` items
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
`<book_dir>/translations_extra.json` — never in this repo.

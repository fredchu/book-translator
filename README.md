# book-translator

A Claude Code skill that turns literary EPUBs into **full-fidelity bilingual EPUBs**: original presentation preserved verbatim (CSS, fonts, images, file paths, class names, internal hrefs), Traditional Chinese (Taiwan) translation paragraphs interleaved after every English content paragraph.

> Not a generic "throw text at an LLM" translator. The pipeline is split between deterministic structural preservation and LLM-driven translation, with five separate audit gates that fail closed.

---

## Cloud GPU (Vast.ai / RunPod)

`scripts/cloud_llm.sh -- --book X.epub` rents a GPU, serves the Qwopus model with vLLM (or SGLang via `CLOUD_LLM_ENGINE=sglang`), translates through the omlx engine and destroys the instance. See SKILL.md "Cloud mode".

Chapters are dispatched concurrently and the safe request width is **measured on the machine you actually rented** — a wave of production-sized chunks is timed at 24, then 16, 12, 8 until one clears both a throughput floor and a slowest-request bound. No GPU model names or VRAM thresholds are involved; `CLOUD_LLM_CONCURRENCY` overrides it.

Measured on an RTX 6000 Ada 48 GB (int4, a 503-request book): **7 minutes and about 0.16 USD per book** at 16 in flight, versus 20 minutes and 0.30 USD at the previous default of 4. Boot is 5–11 minutes and is the dominant cost at that width.

> **Concurrency needs a term table.** Without `spec_terms.json`, concurrent chunks each pick their own transliteration for names — measured worse than sequential, not just no better. The driver warns; it does not block.

## Why this exists

Generic AI book translators tend to do two things badly:

1. **They drop structure.** Cover, title page, copyright, dedication, part dividers, acknowledgments, notes, about-the-author, promotional pages — many of these are short or contain images, so simple text-extract pipelines silently skip them. The output looks like a book but is missing front matter and back matter.
2. **They drop presentation.** Original CSS files, embedded fonts, image assets, and class-driven typography get replaced by a generic stylesheet. The reading experience collapses.

`book-translator` treats structure and presentation as **deterministic contracts**, separate from translation quality. The original EPUB layout (`OEBPS/xhtml/05_Contents.xhtml`, `OEBPS/css/*`, `OEBPS/fonts/*`, `OEBPS/images/*`) is copied verbatim into the output EPUB; translation paragraphs are **inserted alongside** the source paragraphs, with the same class names so original CSS continues to apply.

---

## Pipeline

```
source.epub
   │
   ▼
extract_epub.py           # OPF spine walk → manifest v2 + verbatim copy of
   │                       # css/, fonts/, images/, xhtml/ to <run_dir>/
   ▼
dispatch.py + glossary.py # parallel chapter translation with per-book glossary,
   │                       # style anchor, last-paragraph carryover, cross-chunk
   │                       # coherence — chapter 1 confirms tone before fan-out
   │                       # spec_terms.json (optional, per book) pins agreed
   │                       # renderings; only the terms a chunk actually contains
   │                       # are injected. REQUIRED before enabling concurrency —
   │                       # without it, independent chunks diverge on names
   ▼
assemble.py               # interleave src/tgt paragraphs at original file paths;
   │                       # embed ALL extracted assets; hand-build nav with
   │                       # PART hierarchy
   ▼
bilingual.epub
   │
   ▼
5 deterministic audit gates (each exits non-zero on failure):
  - structural_audit.py            spine completeness
  - bilingual_coverage_audit.py    every English paragraph has Han sibling
  - href_resolve_audit.py          every internal href points to a real entry
  - translation_quality_audit.py   tgt ≥ 30% of src length, no placeholder strings
  - (plus pytest with 338 cases)
```

Output strategies (per-spine-item):

| Strategy | When | Behavior |
|----------|------|----------|
| `translate` | Body chapters, epilogue, acknowledgments | Source + translation paragraphs interleaved |
| `source_only` | Cover, title page, copyright, dedication, part dividers, notes (default), promo | Source xhtml copied verbatim at original path |
| `nav_generated` | Navigation document | Hand-built bilingual nav matching original hierarchy |
| `drop_explicit` | Anything intentionally omitted | Recorded with a non-empty `reason` |

---

## Quality gates

Five orthogonal audits. The first four are deterministic scripts; the fifth is the pytest suite. Any failure stops the run; the bad EPUB is not shipped.

```
$ pytest -q                                          # full suite must be green
$ python scripts/structural_audit.py    --source ... --output ... --book-dir ...
$ python scripts/bilingual_coverage_audit.py  --source ... --output ...
$ python scripts/href_resolve_audit.py        --output ...
$ python scripts/translation_quality_audit.py --output ...
```

`translation_quality_audit.py` specifically catches the failure mode where a translation pipeline silently emits placeholder strings ("translation note: this section preserves...") instead of real translations — it asserts the target paragraph length is at least 30% of the source and bans a list of known placeholder phrases.

---

## Dependencies

Python 3.10+ with:

- `ebooklib`
- `beautifulsoup4`
- `lxml`
- `pytest` (dev only)

No LLM SDK is imported directly. Translation is performed by parallel Claude Code subagents dispatched by `dispatch.py` (Claude Opus 4.7), using a glossary + style anchor + carryover protocol for cross-chunk coherence.

---

## Usage (inside Claude Code)

Trigger phrases the skill responds to:

- 「翻書」「翻電子書」「翻譯整本書」「文學翻譯」
- "translate this novel" / "bilingual epub" / "book translate"
- A direct `.epub` path with literary-translation intent

The skill walks you through glossary confirmation, prints a chapter 1 preview, and asks for tone approval before fanning out. Resume state is preserved in `<run_dir>/state.json`.

---

## Offline path (local models, no API cost)

Besides the Claude-subagent path above, the whole book can be translated by a
local model on Apple Silicon via [omlx](https://github.com/jundot/omlx):

```bash
python3 scripts/translate_book_ollama.py --book /path/to/X.epub --out /path/to/out/
# defaults to: --engine omlx --omlx-model Qwopus3.6-27B-v2-MLX-4bit
```

The default is quality-first, not throughput-first. A faster 35B-A3B MoE was the
default for six weeks on the strength of a 5x speed win and an automated register
score; a full-chapter human read-through found its prose clearly flatter, and the
default reverted. Use `--omlx-model Qwen3.6-35B-Heretic-4bit` when a draft is
enough (~4x faster).

Because the offline path has no glossary pass, `offline_postprocess.py` recovers
what the glossary would have provided — Simplified→Traditional via opencc `s2tw`
applied **sentence by sentence, only to sentences containing an unambiguously Simplified
character** (the `p` variant also swaps mainland vocabulary and corrupts already-correct
Traditional text; even plain `s2tw` mis-resolves one-to-many characters, hence the gate),
character-name coherence, bilingual ToC labels, book-wide gloss dedupe, and
acronym collapse. The last two exist because the model sees one chunk at a time
and treats every chunk as a term's first mention; cross-chunk consistency is
deterministic work, not something a prompt can fix.

**Style rules carry a hard-won warning.** `dispatch.OFFLINE_STYLE_RULES` must
never contain a sentence-length rule. One did, briefly: it scored best of every
candidate on a "share of sentences over 55 characters" metric while collapsing
sentence-length variance and destroying inline source-term glosses. Optimising a
length proxy optimises for monotony. The comment above that constant carries the
measurement table.

---

## Customizing for a specific book

The pipeline itself is book-agnostic. Per-book overrides — extra dedication / copyright / acknowledgments paragraph translations, or custom nav labels for a specific edition — live in `<book_dir>/translations_extra.json`, never in this repo. Schema:

```json
{
  "by_exact_text": {
    "<source paragraph>": "<target translation>"
  },
  "nav_overrides": {
    "<original_idref>": "<custom nav label>"
  }
}
```

The assembler reads this file if present. `by_exact_text` entries win over the generic `STRUCTURAL_LABELS_ZH_TW` dict during paragraph fallback translation; `nav_overrides` entries win during nav label rendering.

A second optional per-book file, `<book_dir>/spec_terms.json`, pins terminology
agreed before translation starts. It is appended to the offline system prompt as
lookup data:

```json
{ "terms": { "private commons": "私人公地", "superagency": "超級能動性" } }
```

Both files are book-specific editorial data and are deliberately not tracked in
this repo.

---

## Project history

- **2026-05-13** — Initial skill plan (`book-translator-skill-plan-2026-05-13.md`)
- **2026-05-14** — First real run on *Co-Intelligence*; structural regressions surfaced; three autonomous fix rounds completed:
  - Round 1: Full OPF spine preservation, explicit `output_strategy` enum, fail-closed assembler
  - Round 2: Verbatim CSS/font/image preservation, original file paths, all internal hrefs resolve
  - Round 3: Real translations for all structural pages, hand-built nav with hierarchy, span-concatenation parser bug fixed

Each round was driven by the [`/automl`](https://github.com/fredchu/claude-automl) skill and dispatched implementation work to OpenAI Codex via the [`codex-dispatch`](https://github.com/fredchu/codex-dispatch) skill.

---

---

# 繁體中文

把文學書 EPUB 翻成**保真中英對照 EPUB** 的 Claude Code skill：原版排版完整保留（CSS、字體、圖片、檔案路徑、class 名、內部連結），每段英文後面插入台灣繁體中文譯文。

> 不是「把文字丟給 LLM 就好」的通用翻譯工具。Pipeline 把「結構保真」和「翻譯品質」拆開處理，由五個獨立、失敗即擋下的 audit gate 把關。

---

## 雲端 GPU（Vast.ai / RunPod）

`scripts/cloud_llm.sh -- --book X.epub` 會租一台 GPU、用 vLLM（或 `CLOUD_LLM_ENGINE=sglang` 換 SGLang）跑 Qwopus 模型、經 omlx 引擎翻譯，翻完砍機。細節見 SKILL.md 的「Cloud mode」。

章節會併發派送，而**同時要送幾個請求是在你實際租到的那台機器上量出來的**——送一波正式大小的稿子，依序試 24、16、12、8，直到某一級同時通過吞吐下限與最慢請求上限為止。過程不使用顯示卡型號或顯存門檻；`CLOUD_LLM_CONCURRENCY` 可以覆蓋。

實測（RTX 6000 Ada 48GB、int4、一本 503 個請求的書）：同時送 16 個時**每本約 7 分鐘、約 0.16 美元**，而先前預設的 4 是 20 分鐘、0.30 美元。開機要 5 到 11 分鐘，在這個併發寬度下開機比翻譯還久。

> **併發需要術語表。** 沒有 `spec_terms.json` 時，併發的每一塊會各自決定人名怎麼音譯——實測比循序**更糟**，不只是沒變好。程式會警告，但不會擋你。

---

## 為什麼做這個

通用 AI 書籍翻譯工具常見兩個問題：

1. **結構掉了**。封面、書名頁、版權頁、獻辭、各部分扉頁、致謝、註釋、作者簡介、推廣頁——這些頁面常常很短或含圖片，純文字抽取的 pipeline 會無聲略過。輸出看起來是一本書，但前後 matter 都消失了。
2. **排版掉了**。原書 CSS、嵌入字體、圖片資產、class 驅動的排版風格被替換成通用樣式，閱讀體驗整個垮掉。

`book-translator` 把結構保真當成**確定性契約**，獨立於翻譯品質。原 EPUB 的版面（`OEBPS/xhtml/05_Contents.xhtml`、`OEBPS/css/*`、`OEBPS/fonts/*`、`OEBPS/images/*`）原樣複製到輸出 EPUB；繁中譯文段落是**接在原文段落後面插入**，沿用同一個 class 名，所以原版 CSS 對譯文也適用。

---

## 流程

```
source.epub
   │
   ▼
extract_epub.py           # 走 OPF spine → manifest v2 + verbatim 複製
   │                       # css/、fonts/、images/、xhtml/ 到 <run_dir>/
   ▼
dispatch.py + glossary.py # 平行翻譯各章，每書一份 glossary、style anchor、
   │                       # 跨章節末段 carryover；第 1 章 preview 確認語感
   │                       # 才 fan out 其餘章節
   │                       # spec_terms.json（選用，每書一份）釘住講好的譯名，
   │                       # 只注入這一塊真的出現的詞。開併發前是必要條件——
   │                       # 沒有它，各自獨立的塊會在人名上分歧
   ▼
assemble.py               # 原檔路徑下交錯 src/tgt 段落；嵌入所有抽出的資產；
   │                       # 手刻 nav 保留 PART 階層
   ▼
bilingual.epub
   │
   ▼
5 個確定性 audit gate（失敗即非 0 exit）：
  - structural_audit.py            spine 完整性
  - bilingual_coverage_audit.py    每段英文都有相鄰漢字段落
  - href_resolve_audit.py          每個內部 href 都對得到實際檔案
  - translation_quality_audit.py   tgt 長度 ≥ src 30%、無 placeholder 字串
  - （加上 pytest 338 個 case）
```

每個 spine item 的 output strategy：

| Strategy | 用在 | 行為 |
|----------|------|------|
| `translate` | 內文章節、Epilogue、Acknowledgments | 原文 + 譯文段落交錯 |
| `source_only` | Cover、書名頁、版權、獻辭、PART 扉頁、Notes（預設）、推廣頁 | 原 xhtml 在原路徑下 verbatim 複製 |
| `nav_generated` | 目錄頁 | 手刻雙語 nav，對照原版階層 |
| `drop_explicit` | 刻意省略的頁面 | 必須附非空 `reason` |

---

## 品質把關

五個正交 audit。前四個是確定性 script，第五個是 pytest。任一失敗就擋下整個流程，不會交付有問題的 EPUB。

```
$ pytest -q                                          # 全套必須綠
$ python scripts/structural_audit.py    --source ... --output ... --book-dir ...
$ python scripts/bilingual_coverage_audit.py  --source ... --output ...
$ python scripts/href_resolve_audit.py        --output ...
$ python scripts/translation_quality_audit.py --output ...
```

`translation_quality_audit.py` 特別擋一種翻譯 pipeline 的常見失敗：偷偷塞 placeholder 字串（像「譯註：本段保留原書出版資訊」）撐版面、看起來通過 coverage 檢查但實際沒翻——這個 audit 強制 tgt 長度至少是 src 的 30%，並列出已知 placeholder 黑名單字串擋下。

---

## 相依

Python 3.10+：

- `ebooklib`
- `beautifulsoup4`
- `lxml`
- `pytest`（開發用）

沒有直接引用 LLM SDK。翻譯是由 `dispatch.py` 派出去的 Claude Code 平行 subagent（Claude Opus 4.7）執行，用 glossary + style anchor + carryover 三件套維持跨段一致性。

---

## 用法（在 Claude Code 內）

觸發詞：

- 「翻書」「翻電子書」「翻譯整本書」「文學翻譯」
- "translate this novel" / "bilingual epub" / "book translate"
- 直接給一個 `.epub` 路徑且暗示要做文學翻譯

skill 會帶你確認 glossary、印出第 1 章 preview 讓你確認語感，再 fan out 翻剩下的章節。中斷後可從 `<run_dir>/state.json` 續跑。

---

## 離線路徑（本機模型，不花 API 費用）

除了上面的 Claude subagent 路徑，整本書也可以用 Apple Silicon 上的本機模型翻，
走 [omlx](https://github.com/jundot/omlx)：

```bash
python3 scripts/translate_book_ollama.py --book /path/to/X.epub --out /path/to/out/
# 預設就是：--engine omlx --omlx-model Qwopus3.6-27B-v2-MLX-4bit
```

**預設是品質優先，不是吞吐優先。** 一顆比較快的 35B-A3B MoE 曾經當了六週預設，
依據是 5 倍速度優勢加上一個自動化的語域評分；後來有人完整讀完一章，判斷它的散文明顯扁平，
預設就換回來了。只要草稿的時候用 `--omlx-model Qwen3.6-35B-Heretic-4bit`（約快 4 倍）。

離線路徑沒有 glossary 那一關，所以 `offline_postprocess.py` 補回 glossary 本來會提供的東西——
簡繁轉換用 opencc `s2tw`，而且是**逐句判斷、只轉含有明確簡體字的句子**
（帶 `p` 的版本還會換大陸詞彙、把本來就正確的繁體改壞；即使是純 `s2tw` 也會把一簡對多繁的字
挑錯，所以要加這道閘）、人名一致化、雙語目錄標籤、全書注釋去重、縮寫收合。
後兩者存在是因為模型一次只看到一塊，每一塊都以為自己是那個詞的第一次出現；
跨塊一致性是確定性的工作，不是 prompt 能修的。

**風格規則帶著一個學費很貴的警告。** `dispatch.OFFLINE_STYLE_RULES` **絕對不可以**放句長規則。
曾經放過一次：它在「超過 55 字的句子佔比」這個指標上是所有候選裡最好的，
但同時把句長變異壓平、把行內術語注釋毀掉。**優化長度的代理指標，等於在優化單調。**
那個常數上方的註解留著完整的量測表。

---

## 針對特定書本客製化

Pipeline 本身是 book-agnostic。書本獨有的覆寫——額外的獻辭 / 版權頁 / 致謝段落譯文，或某個特定版本要用的 nav label——放在 `<book_dir>/translations_extra.json`，**不入 repo**。Schema：

```json
{
  "by_exact_text": {
    "<source paragraph>": "<target translation>"
  },
  "nav_overrides": {
    "<original_idref>": "<custom nav label>"
  }
}
```

Assembler 若偵測到這個檔案會自動讀進來：`by_exact_text` 在段落 fallback 翻譯時優先於 generic `STRUCTURAL_LABELS_ZH_TW`；`nav_overrides` 在 nav label 渲染時覆寫預設邏輯。

---

## 專案歷程

- **2026-05-13** — 初版 skill plan（`book-translator-skill-plan-2026-05-13.md`）
- **2026-05-14** — 第一次實跑 *Co-Intelligence*；結構問題浮現；連跑三輪自動修復：
  - Round 1：完整 OPF spine 保留、`output_strategy` 顯式枚舉、fail-closed assembler
  - Round 2：CSS / 字體 / 圖片 verbatim 保留、原檔路徑、所有內部 href resolve
  - Round 3：所有結構頁都有真譯文、手刻 nav 帶階層、span 黏連 parser bug 修復

每一輪都是由 [`/automl`](https://github.com/fredchu/claude-automl) skill 驅動、透過 [`codex-dispatch`](https://github.com/fredchu/codex-dispatch) skill 派 OpenAI Codex 實作。

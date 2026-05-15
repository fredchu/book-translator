# book-translator

A Claude Code skill that turns literary EPUBs into **full-fidelity bilingual EPUBs**: original presentation preserved verbatim (CSS, fonts, images, file paths, class names, internal hrefs), Traditional Chinese (Taiwan) translation paragraphs interleaved after every English content paragraph.

> Not a generic "throw text at an LLM" translator. The pipeline is split between deterministic structural preservation and LLM-driven translation, with five separate audit gates that fail closed.

---

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
  - (plus pytest with 69 cases)
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
$ pytest -q                                          # 69 passed
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
  - （加上 pytest 69 個 case）
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
$ pytest -q                                          # 69 passed
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

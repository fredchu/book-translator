# /book-translator Ollama + Multi-Model Integration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Date:** 2026-05-22
**Driver:** Fred — Bocky 13 本書翻完後，啟動 v0.2 Ollama fallback + 多模型 benchmark
**Goal:** 加入 Ollama provider 抽象層，接 `translategemma:4b/12b/27b` 與 `Hy-MT2-1.8B/7B/30B-A3B`，在 The Next Renaissance ch.01 跑跨模型品質 benchmark，找出商管科普品類的 cost/quality sweet spot。同時 ship 既有 stability-upgrades plan 的 5 條設計（marker alignment / AUP fallback / prompt purity / Sonnet default / translation_log）。

**Hardware:** Apple M1 Max 32GB unified memory（GPU 共用）

**Phase 順序:**
- **Phase A — Today**：Ollama provider 抽象 + translategemma 接入 + ch.01 三模型 baseline
- **Phase B — Next session**：跑完 `2026-05-19-stability-upgrades.md` 18 tasks（marker / AUP / purity / Sonnet / log）
- **Phase C — Next++**：Hy-MT2 三個模型整合 + 跨 6 模型完整 benchmark

---

## Scope

**In:**
- `~/.claude/skills/book-translator/scripts/providers/` 抽象層（新增 dir）
- `~/.claude/skills/book-translator/scripts/dispatch.py` provider 切換邏輯
- `~/.claude/skills/book-translator/scripts/translate_chapter_cli.py` 單章 CLI（新增）
- `~/.claude/skills/book-translator/SKILL.md` `--engine ollama` 參數文件
- 既有 stability-upgrades plan 18 tasks（Phase B）
- Hy-MT2 三個模型 Modelfile / GGUF 整合（Phase C）

**Out:**
- 大改主 session subagent dispatch 架構（Anthropic 通道仍走 Agent 工具）
- 改 bilingual-book-translator 學長 skill
- Cross-modal eval gate 改動（用既有 Phase 3 machinery 評 Ollama 翻譯品質就好）

---

## Phase A：Ollama Provider 抽象 + translategemma 接入

### Task A1: Provider 抽象層

**Files:**
- Create: `scripts/providers/__init__.py`
- Create: `scripts/providers/base.py` — `TranslationProvider` ABC
- Create: `scripts/providers/anthropic_provider.py` — 現有 Agent 工具走的 path
- Create: `scripts/providers/ollama_provider.py` — `requests` 打 `localhost:11434/api/chat`
- Create: `test/test_providers.py`

**Contract:**

```python
class TranslationProvider(ABC):
    name: str  # "anthropic" / "ollama"
    supports_concurrency: bool  # anthropic=True, ollama=False (single GPU)

    @abstractmethod
    def translate_chapter(
        self,
        *,
        prompt: str,
        chapter_id: str,
        log_dir: Path,
    ) -> ProviderResult:
        """Run a single chapter translation. Returns ProviderResult(raw_text, latency_ms, model, retries)."""
```

`AnthropicProvider`：包現有 `Agent` 工具 dispatch 邏輯（主 session 必須注入，因為 Agent 工具 only 在 CC harness 內可用）—— Anthropic provider 不真的 import Agent，它是「marker class」讓 dispatch.py 知道走 main-session 路徑。

`OllamaProvider`：純 `requests.post("http://localhost:11434/api/chat", json={...})`，stream=False，timeout 600s（27b 慢），retry 3 次（10/30/90s 退避），quota 概念不存在（local 不會撞限）。

### Task A2: dispatch.py 加 --engine flag

- `dispatch.py` 引入 `provider_factory(engine: str) -> TranslationProvider`
- 主 session 設 `provider = provider_factory(args.engine)`，若 `provider.supports_concurrency=False` 強制 `concurrency=1`（log warning）
- `engine='anthropic'`：跑原路徑（fan-out Agent calls）
- `engine='ollama'`：sequential loop，每章 call `provider.translate_chapter(...)`

### Task A3: 單章 CLI（測試用）

`scripts/translate_chapter_cli.py`：給定 epub + chapter index + engine + model，跑單章翻譯吐 stdout，便於跨模型對比腳本呼叫。

```bash
python3 scripts/translate_chapter_cli.py \
  --book "The Next Renaissance.epub" \
  --chapter 1 \
  --engine ollama \
  --ollama-model translategemma:27b \
  --out-dir runs/comparison-ch01/
```

### Task A4: The Next Renaissance ch.01 三模型對比

**Script:** `scripts/run_benchmark.py`

```bash
python3 scripts/run_benchmark.py \
  --book "/Users/fredchu/Documents/For_Claude/inbox/translations/The Next Renaissance_ AI and the Expansion of Human.epub" \
  --models translategemma:4b,translategemma:12b,translategemma:27b \
  --baseline-bocky "/Users/fredchu/Documents/For_Claude/inbox/translations/The Next Renaissance_ AI and the Expansion of Human_bilingual_translated_by_bocky.epub" \
  --baseline-fred "/Users/fredchu/Documents/For_Claude/inbox/translations/the_next_renaissance_bilingual_translated_by_fred.epub" \
  --chapter 1 \
  --out runs/benchmark-2026-05-22/
```

**Output:**
- `runs/benchmark-2026-05-22/ch01_source.txt`
- `runs/benchmark-2026-05-22/ch01_translategemma_4b.txt`
- `runs/benchmark-2026-05-22/ch01_translategemma_12b.txt`
- `runs/benchmark-2026-05-22/ch01_translategemma_27b.txt`
- `runs/benchmark-2026-05-22/ch01_bocky.txt`（從 bilingual epub 抽）
- `runs/benchmark-2026-05-22/ch01_fred.txt`（從 bilingual epub 抽）
- `runs/benchmark-2026-05-22/comparison.md`（並列表格 + latency + 人工 review 區段）

**Eval criteria（先人工，後跑 cross-modal）：**
1. 段對齊（marker / 段數）
2. 商管術語正確（AI / LLM / general purpose tech 等首見並列）
3. 第一人稱保留
4. 語氣對齊原作 register
5. 整體流暢度

---

## Phase B：Stability Upgrades（borrow from Bocky）

執行 `2026-05-19-stability-upgrades.md` 18 tasks，**先做 Phase 1-3**（marker alignment + AUP fallback + prompt purity），這三條對 Ollama 也有用（local model 也會 leak preface、也會 misalign 段）。

- **Phase 1 (Tasks 1-5):** [[PARA_N]] marker 對齊
- **Phase 2 (Tasks 6-8):** AUP refuse fallback（local model 不會 AUP，但保留 mechanism 給 Anthropic）
- **Phase 3 (Tasks 9-12):** Subagent prompt purity（strip leak prefixes）
- **Phase 4 (Tasks 13-15):** Sonnet default + Opus escalation（Anthropic 通道專用）
- **Phase 5 (Tasks 16-17):** Translation log + replay CLI（**對 Ollama 特別有用** — local 跑慢，bad chunk replay 省時間）
- **Phase 6 (Task 18):** Release

**派工建議：** Phase 1-3、5 派 `codex-dispatch` worker mode（plan-driven、deterministic、單檔修改）；Phase 4 主 session（涉及 model 抉擇設計判斷）；Phase 6 主 session（release）。

---

## Phase C：Hy-MT2 三模型整合

**Hy-MT2-1.8B (3.6GB BF16, GGUF 440MB 1.25-bit)：**
- 最容易：抓 GGUF → ollama 自製 Modelfile → `ollama create hy-mt2:1.8b -f Modelfile`
- llama.cpp 不需 PR（1.25-bit 是 STQ kernel 但 GGUF/llama.cpp 主流支援已合）
- VRAM ~4GB，可並行其他

**Hy-MT2-7B (14GB BF16, GGUF 2bit/full)：**
- 中等：抓 GGUF，可能需要 llama.cpp PR #22836 STQ kernel（看 ollama 內建 llama.cpp 版本）
- VRAM ~16GB（full）或 ~5GB（2-bit）

**Hy-MT2-30B-A3B：**
- 困難：MoE 架構，**沒 GGUF**，只能走 transformers/vLLM/SGLang
- Mac M1 Max 走 transformers + bfloat16 + device_map="auto" 應該可行（unified memory 32GB，A3B 表示 active 3B params）
- 不上 ollama，新增 `providers/hf_provider.py` 走 transformers

**Provider 設計：**
- `OllamaProvider`：translategemma 全部 + hy-mt2:1.8b + hy-mt2:7b（若 GGUF OK）
- `HFTransformersProvider`：hy-mt2:30b-a3b（fallback）

---

## Phase A 完成標準（acceptance）

1. `python3 scripts/translate_chapter_cli.py --engine ollama --ollama-model translategemma:4b ...` 跑通，吐出 ch.01 繁中翻譯
2. 三個 translategemma 模型對 The Next Renaissance ch.01 各跑一次，三份 output + latency 全部寫到 `runs/benchmark-2026-05-22/`
3. 比對表 `comparison.md` 有並列段落 + latency 數字 + 人工 5 維度評分
4. 新增 pytest 至少蓋 `OllamaProvider.translate_chapter()` mock 路徑（不 hit 真 ollama）
5. `--engine anthropic`（預設）行為**完全沒變**（regression test）

## Phase B 完成標準

跑完 stability-upgrades 18 tasks 對應的所有測試（從 116 → 預期 145+）；e2e 在 animal_farm fixture 全 audit pass。

## Phase C 完成標準

6 個模型（translategemma × 3 + hy-mt2 × 3 + Opus baseline）對 ch.01 全部跑過，benchmark 表附 cost / latency / 5 維品質 score；推薦 default model 寫進 SKILL.md。

---

## 風險與決策點

| 風險 | 對應 |
|---|---|
| translategemma 對長章節 (>4K tokens) 退化 | Phase A 測 ch.01 後決定要不要 chunk |
| 27b 撞 32GB unified memory wall（其他 process 占用） | sequential 強制 + 跑前 free up memory |
| Hy-MT2-7B GGUF 需 llama.cpp PR 但 ollama 內建版本沒合 | Fallback：自己 build llama.cpp + Modelfile FROM 本地 GGUF |
| MoE 30B-A3B Mac 跑不動 | 降為 vLLM remote 或 skip |
| Provider 抽象層改動破壞既有 e2e | 留 `--engine anthropic` 為 default，新 path opt-in |

---

## 不在這個 plan

- 改 bilingual-book-translator 學長 skill
- Cross-book shared glossary
- Discord 通知
- Launchd scheduler
- 真正 per-chunk Agent dispatch（章內 N subagent call）

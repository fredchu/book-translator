# Ollama Modelfiles for HF-hosted GGUF models

This dir hosts hand-written ollama Modelfiles for translation models pulled from
Hugging Face whose auto-generated Modelfile has a broken chat template.

## When to use

Ollama can pull a GGUF straight from HF:

```
ollama pull hf.co/<org>/<model>-GGUF:<tag>
```

But the auto-generated `TEMPLATE` and stop tokens are sometimes truncated /
mis-quoted (observed on `hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M` — the template
came out as `…{{ end }}onse }}…`, a corrupted `{{ .Response }}` fragment). The
result is that the model reverts to free-form continuation instead of
instruction-following.

The fix is to pull the GGUF and then layer a corrected Modelfile on top via
`ollama create`.

## Hunyuan Translation 2 (Hy-MT2) family

Three sizes — 1.8B, 7B, 30B-A3B. Only 1.8B and 7B are available as GGUF
(the 30B-A3B MoE has no GGUF and exceeds 32GB unified memory on Apple Silicon
even at FP8 — drop it on a 32GB Mac, run it on a server).

Chat format (sourced from `tokenizer_config.json` + `chat_template.jinja` on
`tencent/Hy-MT2-1.8B`):

```
<｜hy_begin▁of▁sentence｜>
[optional system message]<｜hy_place▁holder▁no▁3｜>
<｜hy_User｜>{user content}
<｜hy_Assistant｜>{assistant content}<｜hy_place▁holder▁no▁2｜>
... loop ...
<｜hy_Assistant｜>
```

Stop tokens: `<｜hy_place▁holder▁no▁2｜>` (turn end) and `<｜hy_end▁of▁sentence｜>`.

## Install

```bash
# 1. Pull the GGUF (one-time)
ollama pull hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M
ollama pull hf.co/tencent/Hy-MT2-7B-GGUF:Q4_K_M

# 2. Layer the fixed chat template on top
ollama create hy-mt2:1.8b -f Modelfile.hy-mt2-1.8b
ollama create hy-mt2:7b   -f Modelfile.hy-mt2-7b

# 3. Verify
echo "Translate to Traditional Chinese (Taiwan): Hello" \
  | ollama run hy-mt2:7b
```

After `ollama create`, the named tags `hy-mt2:1.8b` and `hy-mt2:7b` work with
the project's `--ollama-model hy-mt2:7b` flag in
`scripts/translate_chapter_cli.py` and `scripts/run_benchmark.py`.

## Observed quality (benchmark on chapter 6 of *The Next Renaissance*)

| Model | Size | Latency | First-paragraph quality |
|---|---|---|---|
| `hy-mt2:1.8b` (Q4_K_M) | 1.1 GB | 4.3 s | Fast draft. Occasional `人工智能`/`人類智能` mix (CN/TW); occasional hallucinated extra marker block. |
| `hy-mt2:7b` (Q4_K_M) | 4.6 GB | 13.0 s | Comparable to translategemma:27b at 5× the speed. Consistent 台灣繁體中文 (`人工智慧`, `產業`, `數千年`, `存在性問題`). Still translates `AI` → `人工智慧` instead of keeping the abbreviation. |
| `translategemma:27b` | 17 GB | 63.9 s | Baseline reference. |

See `runs/benchmark-hy-mt2/` for the full per-model translations and
`runs/benchmark-2026-05-22/comparison-phase-a-manual.md` for the full eval
matrix.

## STQ kernel note

The Hy-MT2 GGUFs depend on the STQ kernel from llama.cpp PR #22836. Ollama 0.24
already ships a llama.cpp build that handles them — no extra rebuild required
as of 2026-05-22.

## License

Hy-MT2 weights are under Tencent's Hunyuan Community License (see the HF model
card). These Modelfiles are project-local config and do not redistribute weights.

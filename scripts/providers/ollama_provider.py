"""Ollama HTTP provider.

Talks to a local Ollama server (default localhost:11434). Single-threaded —
local GPU can't serve concurrent large-model requests reliably.

Used by both the per-chapter CLI (translate_chapter_cli.py) and the
benchmark script (run_benchmark.py). The main dispatch loop wraps this
provider in a sequential per-chapter loop when --engine=ollama.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import requests

from .base import ProviderError, ProviderResult, TranslationProvider

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_TIMEOUT = 1200  # 27b on a chapter can take ~10 min on M1 Max
RETRY_BACKOFF = [10, 30, 90]


class OllamaProvider(TranslationProvider):
    name = "ollama"
    supports_concurrency = False

    def __init__(
        self,
        model: str,
        *,
        host: str = DEFAULT_HOST,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = 3,
        temperature: float = 0.3,
        num_ctx: int = 32768,
        num_predict: int = 8192,
    ) -> None:
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.temperature = temperature
        self.num_ctx = num_ctx
        self.num_predict = num_predict

    def translate(
        self,
        prompt: str,
        *,
        request_id: str,
        log_dir: Path | None = None,
        system: str | None = None,
    ) -> ProviderResult:
        """Send a chat request. If `system` is provided, prepend it as a system
        role message so the model's chat template separates stable role rules
        (translator persona + marker contract) from the per-chunk user content.
        """
        url = f"{self.host}/api/chat"
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_ctx": self.num_ctx,
                "num_predict": self.num_predict,
            },
            # translategemma / gemma3 base — no <think> tokens; keep raw.
            "think": False,
        }

        start = time.monotonic()
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            attempt_start = time.monotonic()
            try:
                response = requests.post(url, json=payload, timeout=self.timeout)
                response.raise_for_status()
                data = response.json()
                raw_text = data.get("message", {}).get("content", "") or ""
                latency_ms = int((time.monotonic() - start) * 1000)
                attempt_latency_ms = int((time.monotonic() - attempt_start) * 1000)

                if log_dir is not None:
                    log_dir = Path(log_dir)
                    log_dir.mkdir(parents=True, exist_ok=True)
                    log_path = log_dir / f"{request_id}_attempt_{attempt}.json"
                    log_path.write_text(
                        json.dumps(
                            {
                                "request_id": request_id,
                                "model": self.model,
                                "host": self.host,
                                "options": payload["options"],
                                "prompt": prompt,
                                "response_content": raw_text,
                                "response_meta": {
                                    k: v
                                    for k, v in data.items()
                                    if k not in {"message", "content"}
                                },
                                "latency_ms_total": latency_ms,
                                "latency_ms_attempt": attempt_latency_ms,
                                "attempt": attempt,
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )

                return ProviderResult(
                    raw_text=raw_text,
                    model=self.model,
                    latency_ms=latency_ms,
                    retries=attempt,
                    metadata={
                        "eval_count": data.get("eval_count"),
                        "prompt_eval_count": data.get("prompt_eval_count"),
                        "total_duration_ns": data.get("total_duration"),
                        "eval_duration_ns": data.get("eval_duration"),
                        "load_duration_ns": data.get("load_duration"),
                        "prompt_eval_duration_ns": data.get("prompt_eval_duration"),
                    },
                )
            except (requests.RequestException, json.JSONDecodeError) as exc:
                last_err = exc
                if attempt < self.max_retries - 1:
                    sleep_for = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                    time.sleep(sleep_for)

        raise ProviderError(
            f"OllamaProvider({self.model}) failed after {self.max_retries} attempts: {last_err!r}"
        )

    def ping(self) -> bool:
        """Quick health-check against the Ollama server."""
        try:
            r = requests.get(f"{self.host}/api/tags", timeout=5)
            r.raise_for_status()
            return True
        except requests.RequestException:
            return False

"""Omlx OpenAI-compatible HTTP provider.

Talks to a local omlx server (default localhost:8090) or a cloud vLLM/SGLang
endpoint behind the same OpenAI-compatible API (book-translator cloud_llm.sh).

Single-threaded by default — a local single-GPU omlx server can't serve
concurrent large-model requests reliably. Pass `max_concurrent_requests > 1`
(meant for a cloud vLLM/SGLang endpoint doing continuous batching) to set
`supports_concurrency = True`; callers then may dispatch several `translate()`
calls at once from separate threads. `translate()` itself does not mutate
shared state, so that's safe as long as callers don't share other objects.

Used by the per-chapter CLI and the local book driver when --engine=omlx.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import requests

from .base import ProviderError, ProviderResult, TranslationProvider

DEFAULT_HOST = "http://localhost:8090"
DEFAULT_TIMEOUT = 120
# Real full-size 3000-char chunks produced about 509-534 tokens; the observed
# legitimate ceiling is ~650. 2048 leaves >3x output headroom while capping a
# runaway generation at ~77s even at the measured worst N=24 rate (26.6 tok/s),
# safely inside the unchanged 120s request timeout instead of timing out first.
DEFAULT_MAX_TOKENS = 2048
RETRY_BACKOFF = [10, 30, 90]
DEFAULT_CHAT_TEMPLATE_KWARGS = {"enable_thinking": False}


class OmlxProvider(TranslationProvider):
    name = "omlx"
    supports_concurrency = False

    def __init__(
        self,
        model: str,
        *,
        host: str = DEFAULT_HOST,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = 3,
        temperature: float = 0.3,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        chat_template_kwargs: dict[str, Any] | None = None,
        api_key: str | None = None,
        max_concurrent_requests: int = 1,
    ) -> None:
        # api_key：雲端 vLLM（book-translator cloud_llm.sh）用 --api-key 保護公開埠，這裡帶 Bearer。
        # 本機 omlx 不需要，留 None 就完全不送標頭。
        self.api_key = api_key
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.chat_template_kwargs = (
            DEFAULT_CHAT_TEMPLATE_KWARGS.copy()
            if chat_template_kwargs is None
            else dict(chat_template_kwargs)
        )
        # max_concurrent_requests>1 is meant for a cloud vLLM/SGLang endpoint;
        # local omlx stays at the class default (1 -> supports_concurrency=False).
        self.max_concurrent_requests = max(1, max_concurrent_requests)
        self.supports_concurrency = self.max_concurrent_requests > 1

    def translate(
        self,
        prompt: str,
        *,
        request_id: str,
        log_dir: Path | None = None,
        system: str | None = None,
        temperature: float | None = None,
    ) -> ProviderResult:
        """Send a chat completion request to omlx's OpenAI-compatible endpoint.

        `temperature`, when given, is used for this call only — it does not
        touch `self.temperature`, so concurrent callers on separate threads
        can each pick their own attempt temperature without racing.
        """
        url = f"{self.host}/v1/chat/completions"
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        effective_temperature = self.temperature if temperature is None else temperature
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": effective_temperature,
            "chat_template_kwargs": self.chat_template_kwargs,
        }

        start = time.monotonic()
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            attempt_start = time.monotonic()
            try:
                response = requests.post(url, json=payload, timeout=self.timeout, **self._req_kwargs())
                response.raise_for_status()
                data = response.json()
                raw_text = self._extract_content(data)
                finish_reason = data["choices"][0].get("finish_reason")
                if finish_reason == "length":
                    # Do not pass a known-truncated response to marker validation.
                    # Raise immediately (without same-temperature HTTP retries) so
                    # the driver's existing attempt 1 retries at temperature 0.5.
                    raise ProviderError(
                        f"OmlxProvider({self.model}) response truncated at max_tokens={self.max_tokens}"
                    )
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
                                "max_tokens": payload["max_tokens"],
                                "temperature": payload["temperature"],
                                "chat_template_kwargs": payload["chat_template_kwargs"],
                                "prompt": prompt,
                                "response_content": raw_text,
                                "response_meta": {
                                    **{k: v for k, v in data.items() if k not in {"choices"}},
                                    "finish_reason": finish_reason,
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
                        "id": data.get("id"),
                        "object": data.get("object"),
                        "created": data.get("created"),
                        "usage": data.get("usage"),
                        "finish_reason": finish_reason,
                    },
                )
            except (
                requests.RequestException,
                json.JSONDecodeError,
                KeyError,
                IndexError,
                TypeError,
            ) as exc:
                last_err = exc
                if attempt < self.max_retries - 1:
                    sleep_for = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                    time.sleep(sleep_for)

        raise ProviderError(
            f"OmlxProvider({self.model}) failed after {self.max_retries} attempts: {last_err!r}"
        )

    def _req_kwargs(self) -> dict[str, Any]:
        # 沒金鑰就完全不加 headers 參數，讓本機 omlx 的呼叫形狀跟以前一模一樣
        return {"headers": {"Authorization": f"Bearer {self.api_key}"}} if self.api_key else {}

    def ping(self) -> bool:
        """Quick health-check against the omlx server."""
        try:
            r = requests.get(f"{self.host}/v1/models", timeout=5, **self._req_kwargs())
            r.raise_for_status()
            return True
        except requests.RequestException:
            return False

    @staticmethod
    def _extract_content(data: dict[str, Any]) -> str:
        message = data["choices"][0]["message"]
        return message.get("content", "") or ""

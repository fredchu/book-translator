"""Omlx OpenAI-compatible HTTP provider.

Talks to a local omlx server (default localhost:8090). Single-threaded;
local GPU can't serve concurrent large-model requests reliably.

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
        max_tokens: int = 8192,
        chat_template_kwargs: dict[str, Any] | None = None,
    ) -> None:
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

    def translate(
        self,
        prompt: str,
        *,
        request_id: str,
        log_dir: Path | None = None,
        system: str | None = None,
    ) -> ProviderResult:
        """Send a chat completion request to omlx's OpenAI-compatible endpoint."""
        url = f"{self.host}/v1/chat/completions"
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "chat_template_kwargs": self.chat_template_kwargs,
        }

        start = time.monotonic()
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            attempt_start = time.monotonic()
            try:
                response = requests.post(url, json=payload, timeout=self.timeout)
                response.raise_for_status()
                data = response.json()
                raw_text = self._extract_content(data)
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
                                    k: v for k, v in data.items() if k not in {"choices"}
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

    def ping(self) -> bool:
        """Quick health-check against the omlx server."""
        try:
            r = requests.get(f"{self.host}/v1/models", timeout=5)
            r.raise_for_status()
            return True
        except requests.RequestException:
            return False

    @staticmethod
    def _extract_content(data: dict[str, Any]) -> str:
        message = data["choices"][0]["message"]
        return message.get("content", "") or ""

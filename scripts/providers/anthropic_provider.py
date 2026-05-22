"""Anthropic provider — marker class.

The actual Agent tool call happens in the main Claude Code session, which has
access to the `Agent` tool. This class exists so dispatch logic can branch on
`provider.name == "anthropic"` and so the same factory can return either an
Ollama provider (callable directly) or an Anthropic marker (signal to use
Agent tool in main session).
"""

from __future__ import annotations

from pathlib import Path

from .base import ProviderResult, TranslationProvider


class AnthropicProvider(TranslationProvider):
    name = "anthropic"
    supports_concurrency = True

    def __init__(self, model: str = "opus") -> None:
        self.model = model

    def translate(
        self,
        prompt: str,
        *,
        request_id: str,
        log_dir: Path | None = None,
    ) -> ProviderResult:
        raise NotImplementedError(
            "AnthropicProvider.translate must be driven by the main Claude Code "
            "session via the Agent tool. Use this class as a marker (check "
            "provider.name == 'anthropic') and dispatch in the main session "
            "instead of calling this method directly."
        )

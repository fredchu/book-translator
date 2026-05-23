"""Translation provider abstraction.

Lets the book-translator pipeline target different translation engines:

- AnthropicProvider — Claude Code subagent dispatch (main session, parallel).
- OllamaProvider — local Ollama HTTP server (sequential, single GPU).
- OmlxProvider — local omlx OpenAI-compatible HTTP server (sequential, single GPU).
- HFTransformersProvider — Hugging Face Transformers in-process (deferred, MoE models).

Selected at dispatch time via --engine / model CLI flags.
"""

from .base import TranslationProvider, ProviderResult, ProviderError
from .ollama_provider import OllamaProvider
from .omlx_provider import OmlxProvider
from .anthropic_provider import AnthropicProvider

__all__ = [
    "TranslationProvider",
    "ProviderResult",
    "ProviderError",
    "OllamaProvider",
    "OmlxProvider",
    "AnthropicProvider",
    "provider_factory",
]


def provider_factory(engine: str, *, model: str | None = None, **kwargs) -> TranslationProvider:
    """Build a provider by engine name."""
    engine = engine.lower()
    if engine == "anthropic":
        return AnthropicProvider(model=model or "opus")
    if engine == "ollama":
        if not model:
            raise ValueError("--ollama-model is required when --engine=ollama")
        return OllamaProvider(model=model, **kwargs)
    if engine == "omlx":
        if not model:
            raise ValueError("--omlx-model is required when --engine=omlx")
        return OmlxProvider(model=model, **kwargs)
    raise ValueError(f"unknown engine: {engine!r} (expected 'anthropic', 'ollama', or 'omlx')")

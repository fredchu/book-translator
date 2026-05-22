"""Translation provider abstraction.

Lets the book-translator pipeline target different translation engines:

- AnthropicProvider — Claude Code subagent dispatch (main session, parallel).
- OllamaProvider — local Ollama HTTP server (sequential, single GPU).
- HFTransformersProvider — Hugging Face Transformers in-process (deferred, MoE models).

Selected at dispatch time via --engine / --ollama-model CLI flags.
"""

from .base import TranslationProvider, ProviderResult, ProviderError
from .ollama_provider import OllamaProvider
from .anthropic_provider import AnthropicProvider

__all__ = [
    "TranslationProvider",
    "ProviderResult",
    "ProviderError",
    "OllamaProvider",
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
    raise ValueError(f"unknown engine: {engine!r} (expected 'anthropic' or 'ollama')")

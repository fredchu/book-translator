"""Provider ABC + shared result types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ProviderError(RuntimeError):
    """Provider call failed after exhausting retries."""


@dataclass
class ProviderResult:
    raw_text: str
    model: str
    latency_ms: int
    retries: int
    metadata: dict[str, Any] = field(default_factory=dict)


class TranslationProvider(ABC):
    name: str = "base"
    supports_concurrency: bool = False

    @abstractmethod
    def translate(
        self,
        prompt: str,
        *,
        request_id: str,
        log_dir: Path | None = None,
    ) -> ProviderResult:
        """Run a single translation request. Sub-classes raise ProviderError on hard failure."""

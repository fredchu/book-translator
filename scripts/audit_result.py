"""Shared result type for EPUB audit scripts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

AuditStatus = Literal["pass", "fail", "warn"]


@dataclass(frozen=True)
class AuditResult:
    """Canonical result type all audits produce."""

    name: str
    status: AuditStatus
    failures: list[str]
    warnings: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """Warnings remain visible but do not block the audit gate."""
        return self.status in {"pass", "warn"}

    def format_lines(self) -> list[str]:
        lines = [f"{self.name}: {self.status.upper()}"]
        for failure in self.failures:
            lines.append(f"  - {failure}")
        for warning in self.warnings:
            lines.append(f"  - warning: {warning}")
        return lines

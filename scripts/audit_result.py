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
    details: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    def format_lines(self) -> list[str]:
        verdict = "PASS" if self.passed else "FAIL"
        lines = [f"{self.name}: {verdict}"]
        for failure in self.failures:
            lines.append(f"  - {failure}")
        return lines

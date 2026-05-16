"""Tests for audit_result.py."""

from __future__ import annotations

from scripts.audit_result import AuditResult


def test_audit_result_passed_property_and_format_lines():
    result = AuditResult(name="href_resolve", status="pass", failures=[])

    assert result.passed is True
    assert result.format_lines() == ["href_resolve: PASS"]


def test_audit_result_formats_failures():
    result = AuditResult(
        name="translation_quality",
        status="fail",
        failures=["chapter.xhtml: target too short"],
    )

    assert result.passed is False
    assert result.format_lines() == [
        "translation_quality: FAIL",
        "  - chapter.xhtml: target too short",
    ]

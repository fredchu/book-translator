"""Tests for audit_suite.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import assemble
from scripts import audit_suite
from scripts import bilingual_coverage_audit
from scripts import extract_epub
from scripts import href_resolve_audit
from scripts import state
from scripts import structural_audit
from scripts import translation_quality_audit

FIXTURE = Path.home() / "ghkb/interested/bilingual_book_maker/test_books/animal_farm.epub"


def _fake_translate(chapter_html: str) -> str:
    from scripts import dispatch

    return "\n\n".join(
        f"這是足夠長的繁體中文測試譯文：{paragraph}"
        for paragraph in dispatch.html_to_paragraphs(chapter_html)
    )


def _prepared_animal_farm(tmp_path: Path) -> tuple[Path, Path]:
    book_dir = extract_epub.extract(FIXTURE, tmp_path)
    manifest = json.loads((book_dir / "manifest.json").read_text("utf-8"))
    state_data = state.init_state(FIXTURE, manifest["spine"], "zh-tw")
    for entry in manifest["spine"]:
        strategy = entry["output_strategy"]
        if strategy == "translate":
            chapter_html = (book_dir / entry["href"]).read_text("utf-8")
            translation = _fake_translate(chapter_html)
            (book_dir / "chapters" / f"{entry['id']}_translation.txt").write_text(
                translation,
                encoding="utf-8",
            )
            state.mark_done(state_data, entry["id"], translation)
        elif strategy in {"source_only", "nav_generated"}:
            state.mark_source_ready(state_data, entry["id"])
    state.save(book_dir / "state.json", state_data)
    output = tmp_path / "animal_farm_bilingual.epub"
    assemble.assemble(book_dir, output)
    return book_dir, output


@pytest.mark.skipif(not FIXTURE.is_file(), reason=f"fixture missing: {FIXTURE}")
def test_run_all_orders_and_aggregates_animal_farm(tmp_path: Path):
    book_dir, output = _prepared_animal_farm(tmp_path)

    results = audit_suite.run_all(FIXTURE, output, book_dir)

    assert [result.name for result in results] == [
        "structural",
        "bilingual_coverage",
        "translation_quality",
        "href_resolve",
    ]
    assert audit_suite.all_passed(results) is True
    assert audit_suite.format_summary(results).splitlines()[0] == "4 PASS / 0 FAIL"


@pytest.mark.skipif(not FIXTURE.is_file(), reason=f"fixture missing: {FIXTURE}")
def test_run_all_matches_standalone_audit_failures(tmp_path: Path):
    book_dir, output = _prepared_animal_farm(tmp_path)
    results = {result.name: result for result in audit_suite.run_all(FIXTURE, output, book_dir)}

    structural_report = structural_audit.audit(FIXTURE, output, book_dir)
    structural_failures = [
        f"{check['name']}: {detail}"
        for check in structural_report["checks"]
        if check["status"] == "FAIL"
        for detail in check["details"]
    ]
    assert results["structural"].failures == structural_failures
    assert results["structural"].passed == structural_report["passed"]

    for name, standalone in [
        ("bilingual_coverage", bilingual_coverage_audit.audit(FIXTURE, output)),
        ("translation_quality", translation_quality_audit.audit(output)),
        ("href_resolve", href_resolve_audit.audit(output)),
    ]:
        passed, failures = standalone
        assert results[name].passed == passed
        assert results[name].failures == failures

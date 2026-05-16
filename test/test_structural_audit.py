"""Tests for structural_audit.py."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ebooklib import epub

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import assemble  # type: ignore  # noqa: E402
import extract_epub  # type: ignore  # noqa: E402
import state  # type: ignore  # noqa: E402
import structural_audit  # type: ignore  # noqa: E402


FULL_STRUCTURE = Path(__file__).resolve().parent / "fixtures" / "full_structure.epub"


def _prepared_full_structure(tmp_path: Path) -> Path:
    book_dir = extract_epub.extract(FULL_STRUCTURE, tmp_path)
    manifest = json.loads((book_dir / "manifest.json").read_text("utf-8"))
    for entry in manifest["spine"]:
        if entry["output_strategy"] == "translate":
            (book_dir / "chapters" / f"{entry['id']}_translation.txt").write_text(
                f"譯文：{entry['first_heading']}", encoding="utf-8"
            )
    s = state.init_state(FULL_STRUCTURE, manifest["spine"], "zh-tw")
    for entry in manifest["spine"]:
        if entry["output_strategy"] == "translate":
            state.mark_done(s, entry["id"], f"譯文：{entry['first_heading']}")
        elif entry["output_strategy"] in {"source_only", "nav_generated"}:
            state.mark_source_ready(s, entry["id"])
    state.save(book_dir / "state.json", s)
    return book_dir


def test_structural_audit_fails_on_missing_spine_item(tmp_path: Path):
    book_dir = _prepared_full_structure(tmp_path)
    bad = epub.EpubBook()
    bad.set_identifier("bad")
    bad.set_title("Bad")
    bad.set_language("en")
    body = epub.EpubHtml(uid="item_006", file_name="chapters/ch_01.xhtml", title="Chapter 1")
    body.content = "<h1>Chapter 1</h1><p>Only one body item.</p>"
    bad.add_item(body)
    bad.add_item(epub.EpubNcx())
    bad.add_item(epub.EpubNav())
    bad.spine = ["nav", body]
    out = tmp_path / "bad.epub"
    epub.write_epub(str(out), bad)

    report = structural_audit.audit(FULL_STRUCTURE, out, book_dir)
    assert report["passed"] is False
    failed = {check["name"]: check for check in report["checks"] if check["status"] == "FAIL"}
    assert "every_source_spine_item_represented_or_dropped" in failed
    details = "\n".join(failed["every_source_spine_item_represented_or_dropped"]["details"])
    assert "Cover" in details
    assert "PART I" in details


def test_structural_audit_passes_on_full_structure(tmp_path: Path):
    book_dir = _prepared_full_structure(tmp_path)
    out = tmp_path / "full_bilingual.epub"
    assemble.assemble(book_dir, out, strict_nav=False)
    report = structural_audit.audit(FULL_STRUCTURE, out, book_dir)
    assert report["passed"] is True
    assert all(check["status"] == "PASS" for check in report["checks"])

"""Tests for bilingual_coverage_audit.py."""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import bilingual_coverage_audit  # type: ignore  # noqa: E402


def test_bilingual_coverage_audit_fails_on_missing_translation(tmp_path: Path):
    epub_path = tmp_path / "missing.epub"
    _write_epub(
        epub_path,
        '<html><body><p>This is a long English paragraph that clearly needs a Chinese sibling after it for bilingual coverage.</p></body></html>',
    )
    passed, failures = bilingual_coverage_audit.audit(epub_path, epub_path)
    assert passed is False
    assert failures
    assert "missing adjacent zh" in failures[0]


def test_bilingual_coverage_audit_honours_source_only_exception(tmp_path: Path):
    """Paragraphs listed in translations/source_only.json must not be flagged
    as missing-zh; this is the documented escape hatch for endnotes, indexes,
    and other intentionally English-only content."""
    epub_path = tmp_path / "with_exception.epub"
    body_text = "This is a long English paragraph that clearly needs a Chinese sibling after it for bilingual coverage."
    _write_epub(
        epub_path,
        f'<html><body><p>{body_text}</p></body></html>',
    )
    import json as _json
    import shutil

    tmp = epub_path.with_suffix(".tmp")
    shutil.copy(epub_path, tmp)
    with zipfile.ZipFile(epub_path, "r") as src, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.namelist():
            dst.writestr(src.getinfo(item), src.read(item))
        dst.writestr(
            "OEBPS/translations/source_only.json",
            _json.dumps([body_text], ensure_ascii=False),
        )
    shutil.move(tmp, epub_path)

    passed, failures = bilingual_coverage_audit.audit(epub_path, epub_path)
    assert passed is True, f"expected pass, got failures: {failures}"
    assert failures == []


def test_aup_refused_chapter_does_not_fail_bilingual_coverage(tmp_path):
    epub_path = _build_minimal_aup_epub(tmp_path)
    from bilingual_coverage_audit import audit
    ok, failures = audit(epub_path=str(epub_path))
    assert ok, f"audit unexpectedly failed: {failures}"


def _build_minimal_aup_epub(tmp_path: Path) -> Path:
    epub_path = tmp_path / "aup_refused.epub"
    body = (
        '<html><body><div class="aup-refused-note" '
        'style="border-left: 4px solid #c44; padding: 0.5em 1em; margin: 1em 0; '
        'background: #fff4f0; font-size: 0.9em;">'
        '<strong>本章因 LLM 政策拒答，保留原文未譯</strong><br/>'
        "This chapter was refused by the translation LLM's usage policy; "
        "the original English is preserved verbatim. "
        "<em>Reason: Anthropic AUP refused this section</em>"
        "</div>"
        '<p class="src">This is a deliberately long English source paragraph intentionally preserved after an AUP refusal.</p>'
        "</body></html>"
    )
    _write_epub(epub_path, body)
    return epub_path


def _write_epub(path: Path, body: str) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr(
            "META-INF/container.xml",
            """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
""",
        )
        z.writestr(
            "OEBPS/content.opf",
            """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <manifest>
    <item id="c1" href="xhtml/c1.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="c1"/></spine>
</package>
""",
        )
        z.writestr("OEBPS/xhtml/c1.xhtml", body)

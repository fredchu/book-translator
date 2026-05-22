from __future__ import annotations

import json
import sys
import zipfile
import inspect
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import translation_quality_audit  # type: ignore  # noqa: E402


def test_translation_quality_audit_passes_good_pair(tmp_path: Path):
    epub = _write_epub(
        tmp_path,
        '<p class="src">This is a deliberately long English source paragraph for the audit.</p>'
        '<p class="tgt tgt-zh">這是一段足夠長的繁體中文譯文，用來通過品質稽核。</p>',
    )
    passed, failures = translation_quality_audit.audit(epub)
    assert passed
    assert failures == []


def test_translation_quality_audit_uses_configurable_min_length_ratio(tmp_path: Path):
    epub = _write_epub(
        tmp_path,
        f'<p class="src">{"A" * 100}</p>'
        f'<p class="tgt tgt-zh">{"中" * 25}</p>',
    )

    passed, failures = translation_quality_audit.audit(epub, min_length_ratio=0.22)
    strict_passed, strict_failures = translation_quality_audit.audit(epub, min_length_ratio=0.30)

    assert passed
    assert failures == []
    assert not strict_passed
    assert any("target too short" in failure for failure in strict_failures)


def test_translation_quality_audit_has_no_body_path_regex():
    source = Path(inspect.getfile(translation_quality_audit)).read_text("utf-8")

    assert "_is_body_translation_path" not in source
    assert "(06|08|09|10|12|13|14|15|16|17|18)" not in source


def test_translation_quality_audit_fails_banned_placeholder(tmp_path: Path):
    epub = _write_epub(
        tmp_path,
        '<p class="src">This is a deliberately long English source paragraph for the audit.</p>'
        '<p class="tgt tgt-zh">版權頁說明：本段保留原書資訊。</p>',
    )
    passed, failures = translation_quality_audit.audit(epub)
    assert not passed
    assert any("版權頁說明" in failure for failure in failures)


def test_translation_quality_audit_skips_length_ratio_for_headings(tmp_path: Path):
    epub = _write_epub(
        tmp_path,
        '<h1 class="src">CHAPTER 1 The Past Present and Future of Artificial Intelligence</h1>'
        '<p class="tgt tgt-zh">第一章　人工智慧的過去、現在與未來</p>',
    )

    passed, failures = translation_quality_audit.audit(epub, min_length_ratio=0.30)

    assert passed
    assert failures == []


def test_translation_quality_audit_still_checks_banned_patterns_in_headings(tmp_path: Path):
    epub = _write_epub(
        tmp_path,
        '<h1 class="src">CHAPTER 1 The Past Present and Future of Artificial Intelligence</h1>'
        '<p class="tgt tgt-zh">版權頁說明</p>',
    )

    passed, failures = translation_quality_audit.audit(epub, min_length_ratio=0.30)

    assert not passed
    assert any("版權頁說明" in failure for failure in failures)


def test_register_hints_non_fiction_narrative_uses_looser_ratio():
    hints_path = Path(__file__).resolve().parent.parent / "assets" / "register_hints.json"
    data = json.loads(hints_path.read_text("utf-8"))
    ratios = {register["id"]: register["min_length_ratio"] for register in data["registers"]}

    assert ratios == {
        "literary_fiction": 0.22,
        "non_fiction_narrative": 0.22,
        "academic_technical": 0.30,
    }


def test_translation_quality_audit_fails_missing_unlisted_target(tmp_path: Path):
    epub = _write_epub(
        tmp_path,
        '<p class="src">This is a deliberately long English source paragraph for the audit.</p>',
    )
    passed, failures = translation_quality_audit.audit(epub)
    assert not passed
    assert any("source-only paragraph not in exceptions" in failure for failure in failures)


def test_aup_refused_chapter_does_not_fail_translation_quality_audit(tmp_path):
    """A chapter containing the AUP refused note + source-only paragraphs
    should not trip the translation-quality audit's 'unlisted source-only
    paragraph' or 'too-short translation' checks."""
    # Build a minimal EPUB fixture with one chapter that has the AUP note block
    # ... (test scaffolding — write an EPUB zip with one chapter)
    epub_path = _build_minimal_aup_epub(tmp_path)
    from translation_quality_audit import audit
    ok, failures = audit(epub_path=str(epub_path))
    assert ok, f"audit unexpectedly failed: {failures}"


def _build_minimal_aup_epub(tmp_path: Path) -> Path:
    body = (
        '<div class="aup-refused-note" '
        'style="border-left: 4px solid #c44; padding: 0.5em 1em; margin: 1em 0; '
        'background: #fff4f0; font-size: 0.9em;">'
        '<strong>本章因 LLM 政策拒答，保留原文未譯</strong><br/>'
        "This chapter was refused by the translation LLM's usage policy; "
        "the original English is preserved verbatim. "
        "<em>Reason: Anthropic AUP refused this section</em>"
        "</div>"
        '<p class="src">This is a deliberately long English source paragraph intentionally preserved after an AUP refusal.</p>'
    )
    return _write_epub(tmp_path, body)


def _write_epub(tmp_path: Path, body: str) -> Path:
    epub = tmp_path / "book.epub"
    with zipfile.ZipFile(epub, "w") as z:
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
            """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <manifest>
    <item id="chap" href="xhtml/chapter.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="chap"/></spine>
</package>
""",
        )
        z.writestr("OEBPS/xhtml/chapter.xhtml", f"<html><body>{body}</body></html>")
    return epub

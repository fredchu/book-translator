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


def test_han_decoration_would_hide_an_english_echo(tmp_path: Path):
    honest = tmp_path / "honest.epub"
    decorated = tmp_path / "decorated.epub"
    source = "This is a long ordinary English prose paragraph that the model echoed instead of translating into Chinese."
    _write_epub(
        honest,
        f'<html><body><p class="src">{source}</p><p class="tgt tgt-zh">{source}</p></body></html>',
    )
    _write_epub(
        decorated,
        f'<html><body><p class="src">{source}</p><p class="tgt tgt-zh">譯文：{source}</p></body></html>',
    )

    assert bilingual_coverage_audit.audit(honest, honest)[0] is False
    assert bilingual_coverage_audit.audit(decorated, decorated)[0] is True


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
            _json.dumps(
                [{"src_text": body_text, "reason": "bibliography"}],
                ensure_ascii=False,
            ),
        )
    shutil.move(tmp, epub_path)

    passed, failures = bilingual_coverage_audit.audit(epub_path, epub_path)
    assert passed is True, f"expected pass, got failures: {failures}"
    assert failures == []


def test_bilingual_coverage_rejects_unreasoned_source_only_string(tmp_path: Path):
    epub_path = tmp_path / "unreasoned.epub"
    body_text = "This long English prose paragraph is untranslated and must not be exempted merely because its text appears in a list."
    _write_epub(epub_path, f'<html><body><p>{body_text}</p></body></html>')
    import json as _json
    import shutil

    tmp = epub_path.with_suffix(".tmp")
    shutil.copy(epub_path, tmp)
    with zipfile.ZipFile(epub_path, "r") as src, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.namelist():
            dst.writestr(src.getinfo(item), src.read(item))
        dst.writestr("OEBPS/translations/source_only.json", _json.dumps([body_text]))
    shutil.move(tmp, epub_path)

    passed, failures = bilingual_coverage_audit.audit(epub_path, epub_path)
    assert not passed
    assert any("missing adjacent zh" in failure for failure in failures)


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


# --- regression: the bilingual ToC renders both languages inside one node -----
# `nav_builder` writes "Title Page ｜ 書名頁" as a single <a> inside one <p
# class="src">. The old coverage universe admitted only Han-free English so it
# never saw these; widening it to "marked src and has latin" pulled them in and
# demanded a zh sibling for a node that is already bilingual — Superagency's
# contents page went to 6 failures and The Meaning of Your Life's to 3, every
# one of them reading "missing adjacent zh after: Notes ｜ 註釋".
#
# The guard is one line in _source_nodes_requiring_coverage (skip nodes that
# already contain Han). It shipped with no test: flipping it to `if False`
# left all 427 tests green. This is that test.

INLINE_BILINGUAL_NAV = """<html><body>
<p class="con src"><a href="a.xhtml">Title Page ｜ 書名頁</a></p>
<p class="con src"><a href="b.xhtml">Notes ｜ 註釋</a></p>
<p class="con src"><a href="c.xhtml">Copyright ｜ 版權頁</a></p>
</body></html>"""


def _nodes_requiring_coverage(html: str):
    from bs4 import BeautifulSoup

    return bilingual_coverage_audit._source_nodes_requiring_coverage(
        BeautifulSoup(html, "html.parser")
    )


def test_inline_bilingual_nav_link_is_not_asked_for_a_translation() -> None:
    """A node carrying its own Chinese must not be told it is missing Chinese."""
    assert _nodes_requiring_coverage(INLINE_BILINGUAL_NAV) == []


def test_english_only_nav_link_is_still_checked() -> None:
    """The exclusion keys on Han characters, not on being a nav link."""
    html = '<html><body><p class="con src"><a href="a.xhtml">Title Page</a></p></body></html>'
    assert len(_nodes_requiring_coverage(html)) == 1

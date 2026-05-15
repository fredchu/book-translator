from __future__ import annotations

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


def test_translation_quality_audit_fails_missing_unlisted_target(tmp_path: Path):
    epub = _write_epub(
        tmp_path,
        '<p class="src">This is a deliberately long English source paragraph for the audit.</p>',
    )
    passed, failures = translation_quality_audit.audit(epub)
    assert not passed
    assert any("source-only paragraph not in exceptions" in failure for failure in failures)


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

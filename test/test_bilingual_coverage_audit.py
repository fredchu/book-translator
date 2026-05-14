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

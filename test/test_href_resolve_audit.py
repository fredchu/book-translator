"""Tests for href_resolve_audit.py."""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import href_resolve_audit  # type: ignore  # noqa: E402


def test_href_resolve_audit_fails_on_broken_link(tmp_path: Path):
    epub_path = tmp_path / "broken.epub"
    with zipfile.ZipFile(epub_path, "w") as z:
        z.writestr("OEBPS/xhtml/c1.xhtml", '<html><body><a href="missing.xhtml">Broken</a></body></html>')
    passed, broken = href_resolve_audit.audit(epub_path)
    assert passed is False
    assert broken == ["OEBPS/xhtml/c1.xhtml: missing.xhtml -> OEBPS/xhtml/missing.xhtml"]

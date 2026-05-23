from __future__ import annotations

import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import translation_quality_audit  # type: ignore  # noqa: E402


def test_contains_simplified_chinese_returns_false_for_traditional_text() -> None:
    text = "這是一段使用繁體中文的自然譯文，完全符合台灣用語。"

    assert not translation_quality_audit.contains_simplified_chinese(text)


def test_contains_simplified_chinese_returns_true_for_simplified_text() -> None:
    text = "这是一个已经发生变化的系统，我们应该进行学习。"

    assert translation_quality_audit.contains_simplified_chinese(text)


def test_contains_simplified_chinese_returns_true_for_mixed_text() -> None:
    text = "這是一段繁體譯文，但混入这和学两个簡體字。"

    assert translation_quality_audit.contains_simplified_chinese(text)


def test_contains_simplified_chinese_skips_short_text_under_threshold() -> None:
    assert not translation_quality_audit.contains_simplified_chinese("第六章 学")


def test_translation_quality_audit_fails_tgt_paragraph_with_simplified_text(tmp_path: Path) -> None:
    epub = _write_epub(
        tmp_path,
        '<p class="src">This is a deliberately long English source paragraph for the audit.</p>'
        '<p class="tgt tgt-zh">这是一个已经发生变化的系统，我们应该进行学习，而且不能混入简体字。</p>',
    )

    passed, failures = translation_quality_audit.audit(epub, min_length_ratio=0.0)

    assert not passed
    assert any("error: target contains Simplified Chinese characters" in failure for failure in failures)
    assert any("这" in failure and "学" in failure for failure in failures)


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

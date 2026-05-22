from __future__ import annotations

import sys
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import run_benchmark  # type: ignore  # noqa: E402
from providers.base import ProviderError, ProviderResult  # type: ignore  # noqa: E402


def _make_minimal_epub(tmp_path: Path) -> Path:
    """Create a 1-chapter EPUB whose spine_xhtml_paths() returns ['OEBPS/ch.xhtml']."""
    epub = tmp_path / "fixture.epub"
    container = b'''<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/book.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>'''
    opf = b'''<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">test</dc:identifier>
    <dc:title>Test</dc:title>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="ch" href="ch.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="ch"/>
  </spine>
</package>'''
    chapter = b'''<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Hello world.</p><p>Second paragraph.</p></body></html>'''
    with zipfile.ZipFile(epub, "w") as z:
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/book.opf", opf)
        z.writestr("OEBPS/ch.xhtml", chapter)
    return epub


def test_missing_book_arg_exits(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        run_benchmark.main(["--models", "x:1", "--out", str(tmp_path / "out")])


def test_missing_out_arg_exits(tmp_path: Path) -> None:
    epub = _make_minimal_epub(tmp_path)

    with pytest.raises(SystemExit):
        run_benchmark.main(["--book", str(epub), "--models", "x:1"])


def test_parse_chapters() -> None:
    assert run_benchmark._parse_chapters("5,6,7") == [5, 6, 7]


def test_parse_models() -> None:
    assert run_benchmark._parse_models("a:1,b:2") == ["a:1", "b:2"]


@patch("run_benchmark.OllamaProvider")
def test_end_to_end_mocked_ollama_writes_outputs(
    mock_provider_class: MagicMock,
    tmp_path: Path,
) -> None:
    epub = _make_minimal_epub(tmp_path)
    out_dir = tmp_path / "out"
    provider = mock_provider_class.return_value
    provider.ping.return_value = True
    provider.translate.return_value = ProviderResult(
        raw_text="MOCKED 中文",
        model="x:1",
        latency_ms=100,
        retries=0,
        metadata={},
    )

    exit_code = run_benchmark.main(
        [
            "--book",
            str(epub),
            "--models",
            "x:1,y:2",
            "--chapters",
            "1",
            "--out",
            str(out_dir),
        ]
    )

    assert exit_code == 0
    assert (out_dir / "ch01_x_1.txt").read_text(encoding="utf-8") == "MOCKED 中文"
    assert (out_dir / "ch01_y_2.txt").exists()
    comparison = (out_dir / "comparison.md").read_text(encoding="utf-8")
    assert "x:1" in comparison
    assert "y:2" in comparison
    assert "ch01" in comparison


@patch("run_benchmark.OllamaProvider")
def test_provider_error_does_not_crash_and_marks_failed(
    mock_provider_class: MagicMock,
    tmp_path: Path,
) -> None:
    epub = _make_minimal_epub(tmp_path)
    out_dir = tmp_path / "out"
    provider = mock_provider_class.return_value
    provider.ping.return_value = True
    provider.translate.side_effect = ProviderError("boom")

    exit_code = run_benchmark.main(
        [
            "--book",
            str(epub),
            "--models",
            "x:1",
            "--chapters",
            "1",
            "--out",
            str(out_dir),
        ]
    )

    assert exit_code == 0
    comparison = (out_dir / "comparison.md").read_text(encoding="utf-8")
    assert "FAILED" in comparison
    assert "x:1" in comparison

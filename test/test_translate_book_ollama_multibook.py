from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import translate_book_ollama as drv  # type: ignore  # noqa: E402


def test_multibook_argparse_accepts_multiple_books() -> None:
    args = drv.build_parser().parse_args(["--book", "a.epub", "b.epub"])

    assert args.book == [Path("a.epub"), Path("b.epub")]


def test_multibook_single_book_backward_compat() -> None:
    args = drv.build_parser().parse_args(["--book", "a.epub"])

    assert args.book == [Path("a.epub")]


def test_multibook_repeated_book_flags_are_flattened() -> None:
    args = drv.build_parser().parse_args(["--book", "a.epub", "--book", "b.epub"])

    assert args.book == [Path("a.epub"), Path("b.epub")]


def test_multibook_error_containment_continues(monkeypatch, tmp_path, capsys) -> None:
    attempted: list[Path] = []

    def fake_translate_single_book(book_path: Path, args) -> dict[str, object]:
        attempted.append(book_path)
        if book_path.name == "first.epub":
            raise RuntimeError("boom")
        return {
            "path": book_path,
            "status": "success",
            "duration_sec": 1.25,
            "error_summary": "",
            "return_code": 0,
        }

    translate_mock = MagicMock(side_effect=fake_translate_single_book)
    monkeypatch.setattr(drv, "translate_single_book", translate_mock)

    rc = drv.main(["--book", "first.epub", "second.epub", "--out", str(tmp_path)])

    assert rc == 1
    assert attempted == [Path("first.epub"), Path("second.epub")]
    assert translate_mock.call_count == 2
    captured = capsys.readouterr()
    assert "[ERROR first.epub] boom" in captured.err
    assert "=== Summary === 1/2 books succeeded" in captured.err

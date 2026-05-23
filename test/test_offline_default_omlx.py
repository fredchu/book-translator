"""Lock in the offline-default contract — `--engine omlx` + Qwopus3.6 MLX.

2026-05-23 promoted omlx + Qwopus3.6-27B-v2-MLX-4bit to the default offline
backend (replacing the prior ollama + hy-mt2:7b default). These tests fail-loud
if someone reverts either default without intent.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))


def _module_source(module_name: str) -> str:
    """Import the script module and return its source text."""
    mod = __import__(module_name)
    file_path = getattr(mod, "__file__", None)
    assert file_path is not None, f"{module_name} has no __file__ — cannot read source"
    return Path(file_path).read_text(encoding="utf-8")


def test_book_driver_default_engine_is_omlx() -> None:
    source = _module_source("translate_book_ollama")
    assert 'choices=["ollama", "omlx"], default="omlx"' in source, (
        "translate_book_ollama.py --engine default must be omlx (offline-default contract)"
    )


def test_book_driver_default_omlx_model_is_qwopus_27b() -> None:
    source = _module_source("translate_book_ollama")
    assert 'default="Qwopus3.6-27B-v2-MLX-4bit"' in source, (
        "translate_book_ollama.py --omlx-model default must be Qwopus3.6-27B-v2-MLX-4bit"
    )


def test_chapter_cli_default_engine_is_omlx() -> None:
    source = _module_source("translate_chapter_cli")
    assert 'default="omlx"' in source, (
        "translate_chapter_cli.py --engine default must be omlx"
    )


def test_chapter_cli_default_omlx_model_is_qwopus_27b() -> None:
    source = _module_source("translate_chapter_cli")
    assert 'default="Qwopus3.6-27B-v2-MLX-4bit"' in source, (
        "translate_chapter_cli.py --omlx-model default must be Qwopus3.6-27B-v2-MLX-4bit"
    )

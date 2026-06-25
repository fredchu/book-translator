from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import translate_chapter_cli as cli  # type: ignore  # noqa: E402
from providers import OmlxProvider  # type: ignore  # noqa: E402
from providers.base import ProviderResult  # type: ignore  # noqa: E402


def test_omlx_cli_flags_build_provider_and_write_outputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider = MagicMock()
    provider.ping.return_value = True
    provider.translate.return_value = ProviderResult(
        raw_text="[[PARA_1]]\n譯文",
        model="Qwopus3.6-27B-v2-MLX-4bit",
        latency_ms=10,
        retries=0,
        metadata={},
    )
    calls: list[dict] = []

    def fake_provider_factory(engine: str, *, model: str | None = None, **kwargs):
        calls.append({"engine": engine, "model": model, "kwargs": kwargs})
        return provider

    monkeypatch.setattr(cli, "provider_factory", fake_provider_factory)
    monkeypatch.setattr(cli, "_read_chapter_html", lambda _book, _chapter: ("<p>Hello.</p>", "ch.xhtml"))
    monkeypatch.setattr(cli, "_build_prompt", lambda *_args, **_kwargs: "prompt")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "translate_chapter_cli.py",
            "--book",
            str(tmp_path / "book.epub"),
            "--chapter",
            "6",
            "--engine",
            "omlx",
            "--omlx-model",
            "Qwopus3.6-27B-v2-MLX-4bit",
            "--omlx-host",
            "http://127.0.0.1:8099",
            "--out",
            str(tmp_path / "out"),
            "--timeout",
            "600",
            "--num-predict",
            "2048",
            "--temperature",
            "0.2",
        ],
    )

    assert cli.main() == 0

    assert calls == [
        {
            "engine": "omlx",
            "model": "Qwopus3.6-27B-v2-MLX-4bit",
            "kwargs": {
                "host": "http://127.0.0.1:8099",
                "timeout": 600,
                "max_tokens": 2048,
                "temperature": 0.2,
            },
        }
    ]
    out_file = tmp_path / "out" / "ch06_Qwopus3.6-27B-v2-MLX-4bit.txt"
    assert out_file.read_text(encoding="utf-8") == "[[PARA_1]]\n譯文"


def test_omlx_cli_uses_default_model_when_omitted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """2026-06-25: --omlx-model defaults to Qwen3.6-35B-Heretic-4bit, and
    --engine defaults to omlx. Previously this test asserted SystemExit when
    --omlx-model was omitted; after the default promotion the parser no longer
    raises. Confirm both defaults flow through to the provider factory.
    """
    provider = MagicMock()
    provider.ping.return_value = True
    provider.translate.return_value = ProviderResult(
        raw_text="[[PARA_1]]\n譯文",
        model="Qwopus3.6-27B-v2-MLX-4bit",
        latency_ms=10,
        retries=0,
        metadata={},
    )
    calls: list[dict] = []

    def fake_provider_factory(engine: str, *, model: str | None = None, **kwargs):
        calls.append({"engine": engine, "model": model, "kwargs": kwargs})
        return provider

    monkeypatch.setattr(cli, "provider_factory", fake_provider_factory)
    monkeypatch.setattr(cli, "_read_chapter_html", lambda _book, _chapter: ("<p>Hello.</p>", "ch.xhtml"))
    monkeypatch.setattr(cli, "_build_prompt", lambda *_args, **_kwargs: "prompt")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "translate_chapter_cli.py",
            "--book",
            str(tmp_path / "book.epub"),
            # NO --engine, NO --omlx-model — both should default
            "--out",
            str(tmp_path / "out"),
        ],
    )

    assert cli.main() == 0
    assert len(calls) == 1
    assert calls[0]["engine"] == "omlx"
    assert calls[0]["model"] == "Qwen3.6-35B-Heretic-4bit"


def test_live_omlx_ping_smoke_skips_unless_enabled() -> None:
    if os.environ.get("RUN_OMLX_INTEGRATION") != "1":
        pytest.skip("set RUN_OMLX_INTEGRATION=1 to run live omlx smoke")
    try:
        requests.get("http://localhost:8090/v1/models", timeout=3).raise_for_status()
    except requests.RequestException as exc:
        pytest.skip(f"omlx unreachable: {exc}")

    assert OmlxProvider("Qwopus3.6-27B-v2-MLX-4bit").ping() is True

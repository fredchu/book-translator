from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from providers import AnthropicProvider, OllamaProvider, provider_factory  # type: ignore  # noqa: E402
from providers.base import ProviderError  # type: ignore  # noqa: E402


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("providers.ollama_provider.time.sleep", lambda *_: None)


def test_provider_factory_anthropic_defaults_to_opus() -> None:
    provider = provider_factory("anthropic")

    assert isinstance(provider, AnthropicProvider)
    assert provider.model == "opus"


def test_provider_factory_ollama_uses_model() -> None:
    provider = provider_factory("ollama", model="translategemma:4b")

    assert isinstance(provider, OllamaProvider)
    assert provider.model == "translategemma:4b"


def test_provider_factory_ollama_requires_model() -> None:
    with pytest.raises(ValueError):
        provider_factory("ollama")


def test_provider_factory_unknown_engine_raises() -> None:
    with pytest.raises(ValueError):
        provider_factory("nonsense")


@patch("providers.ollama_provider.requests.post")
def test_ollama_translate_temperature_override_does_not_mutate_self(mock_post: MagicMock) -> None:
    """Symmetric with OmlxProvider: _translate_chunk in translate_book_ollama.py
    calls both provider types the same way, so both must accept a per-call
    temperature override without mutating shared instance state."""
    response = MagicMock()
    response.json.return_value = {"message": {"content": "abc"}}
    response.raise_for_status.return_value = None
    mock_post.return_value = response

    provider = OllamaProvider("translategemma:4b", temperature=0.3)
    provider.translate("prompt", request_id="req", temperature=0.9)

    assert mock_post.call_args.kwargs["json"]["options"]["temperature"] == 0.9
    assert provider.temperature == 0.3  # unchanged


@patch("providers.ollama_provider.requests.post")
def test_ollama_translate_happy_path(mock_post: MagicMock) -> None:
    response = MagicMock()
    response.json.return_value = {"message": {"content": "abc"}, "eval_count": 42}
    response.raise_for_status.return_value = None
    mock_post.return_value = response

    result = OllamaProvider("translategemma:4b").translate("prompt", request_id="req")

    assert result.raw_text == "abc"
    assert result.model == "translategemma:4b"
    assert result.retries == 0
    assert result.latency_ms >= 0
    assert result.metadata["eval_count"] == 42


@patch("providers.ollama_provider.requests.post")
def test_ollama_translate_retries_timeout_then_raises(mock_post: MagicMock) -> None:
    mock_post.side_effect = requests.Timeout("timed out")

    with pytest.raises(ProviderError):
        OllamaProvider("translategemma:4b", max_retries=3).translate("prompt", request_id="req")

    assert mock_post.call_count == 3


@patch("providers.ollama_provider.requests.post")
def test_ollama_translate_retries_500_then_raises(mock_post: MagicMock) -> None:
    response = MagicMock()
    response.raise_for_status.side_effect = requests.HTTPError("500")
    mock_post.return_value = response

    with pytest.raises(ProviderError):
        OllamaProvider("translategemma:4b", max_retries=3).translate("prompt", request_id="req")

    assert mock_post.call_count == 3


@patch("providers.ollama_provider.requests.post")
def test_ollama_translate_log_dir_writes_attempt_json(mock_post: MagicMock, tmp_path: Path) -> None:
    response = MagicMock()
    response.json.return_value = {"message": {"content": "abc"}, "eval_count": 42}
    response.raise_for_status.return_value = None
    mock_post.return_value = response

    result = OllamaProvider("translategemma:4b").translate(
        "prompt",
        request_id="req",
        log_dir=tmp_path,
    )

    log_path = tmp_path / "req_attempt_0.json"
    data = json.loads(log_path.read_text(encoding="utf-8"))
    assert result.raw_text == "abc"
    assert data["prompt"] == "prompt"
    assert data["response_content"] == "abc"
    assert data["latency_ms_total"] >= 0
    assert data["attempt"] == 0


@patch("providers.ollama_provider.requests.get")
def test_ollama_ping_returns_true_on_200(mock_get: MagicMock) -> None:
    response = MagicMock()
    response.raise_for_status.return_value = None
    mock_get.return_value = response

    assert OllamaProvider("translategemma:4b").ping() is True


@patch("providers.ollama_provider.requests.get")
def test_ollama_ping_returns_false_on_connection_error(mock_get: MagicMock) -> None:
    mock_get.side_effect = requests.ConnectionError("down")

    assert OllamaProvider("translategemma:4b").ping() is False


def test_anthropic_translate_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        AnthropicProvider().translate("prompt", request_id="req")

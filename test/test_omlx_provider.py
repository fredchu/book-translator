from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from providers import OmlxProvider, provider_factory  # type: ignore  # noqa: E402
from providers.base import ProviderError  # type: ignore  # noqa: E402


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("providers.omlx_provider.time.sleep", lambda *_: None)


def _response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def test_provider_factory_omlx_uses_model() -> None:
    provider = provider_factory("omlx", model="Qwopus3.6-27B-v2-MLX-4bit")

    assert isinstance(provider, OmlxProvider)
    assert provider.model == "Qwopus3.6-27B-v2-MLX-4bit"


def test_provider_factory_omlx_requires_model() -> None:
    with pytest.raises(ValueError):
        provider_factory("omlx")


@patch("providers.omlx_provider.requests.post")
def test_omlx_translate_request_shape_defaults_disable_thinking(mock_post: MagicMock) -> None:
    mock_post.return_value = _response(
        {"choices": [{"message": {"content": "譯文", "reasoning_content": "ignored"}}]}
    )

    result = OmlxProvider("model-a", max_tokens=123, timeout=456).translate(
        "prompt",
        request_id="req",
        system="rules",
    )

    assert result.raw_text == "譯文"
    mock_post.assert_called_once()
    url = mock_post.call_args.args[0]
    payload = mock_post.call_args.kwargs["json"]
    assert url == "http://localhost:8090/v1/chat/completions"
    assert mock_post.call_args.kwargs["timeout"] == 456
    assert payload == {
        "model": "model-a",
        "messages": [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "prompt"},
        ],
        "max_tokens": 123,
        "temperature": 0.3,
        "chat_template_kwargs": {"enable_thinking": False},
    }


@patch("providers.omlx_provider.requests.post")
def test_omlx_translate_parses_content_and_ignores_reasoning(mock_post: MagicMock) -> None:
    mock_post.return_value = _response(
        {
            "id": "cmpl-1",
            "choices": [
                {
                    "message": {
                        "content": "translated only",
                        "reasoning_content": "<think>slow private chain</think>",
                    }
                }
            ],
            "usage": {"completion_tokens": 3},
        }
    )

    result = OmlxProvider("model-a").translate("prompt", request_id="req")

    assert result.raw_text == "translated only"
    assert result.metadata["id"] == "cmpl-1"
    assert result.metadata["usage"] == {"completion_tokens": 3}


@patch("providers.omlx_provider.requests.post")
def test_omlx_translate_retries_malformed_response_then_raises(mock_post: MagicMock) -> None:
    mock_post.return_value = _response({"choices": []})

    with pytest.raises(ProviderError):
        OmlxProvider("model-a", max_retries=3).translate("prompt", request_id="req")

    assert mock_post.call_count == 3


@patch("providers.omlx_provider.requests.post")
def test_omlx_translate_log_dir_writes_attempt_json(mock_post: MagicMock, tmp_path: Path) -> None:
    mock_post.return_value = _response({"choices": [{"message": {"content": "abc"}}]})

    result = OmlxProvider("model-a", host="http://127.0.0.1:8099/").translate(
        "prompt",
        request_id="req",
        log_dir=tmp_path,
    )

    log_path = tmp_path / "req_attempt_0.json"
    data = json.loads(log_path.read_text(encoding="utf-8"))
    assert result.raw_text == "abc"
    assert data["host"] == "http://127.0.0.1:8099"
    assert data["prompt"] == "prompt"
    assert data["response_content"] == "abc"
    assert data["chat_template_kwargs"] == {"enable_thinking": False}


@patch("providers.omlx_provider.requests.get")
def test_omlx_ping_uses_models_endpoint_with_host_override(mock_get: MagicMock) -> None:
    response = MagicMock()
    response.raise_for_status.return_value = None
    mock_get.return_value = response

    assert OmlxProvider("model-a", host="http://127.0.0.1:8099/").ping() is True

    mock_get.assert_called_once_with("http://127.0.0.1:8099/v1/models", timeout=5)


@patch("providers.omlx_provider.requests.get")
def test_omlx_ping_returns_false_on_connection_error(mock_get: MagicMock) -> None:
    mock_get.side_effect = requests.ConnectionError("down")

    assert OmlxProvider("model-a").ping() is False

from __future__ import annotations

import concurrent.futures as cf
import json
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from providers import OmlxProvider, provider_factory  # type: ignore  # noqa: E402
from providers.base import ProviderError  # type: ignore  # noqa: E402
from providers.omlx_provider import DEFAULT_MAX_TOKENS, DEFAULT_TIMEOUT  # type: ignore  # noqa: E402


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


def test_omlx_default_max_tokens_has_measured_headroom_and_finishes_before_timeout() -> None:
    provider = OmlxProvider("model-a")
    observed_legitimate_ceiling = 650
    worst_measured_tokens_per_second = 26.6
    assert provider.max_tokens == DEFAULT_MAX_TOKENS == 2048
    assert DEFAULT_MAX_TOKENS >= 2 * observed_legitimate_ceiling
    assert DEFAULT_TIMEOUT >= 2 * (observed_legitimate_ceiling / worst_measured_tokens_per_second)
    assert DEFAULT_MAX_TOKENS / worst_measured_tokens_per_second < DEFAULT_TIMEOUT


def test_omlx_default_does_not_support_concurrency() -> None:
    """Local single-GPU omlx stays sequential unless opted in."""
    provider = OmlxProvider("model-a")
    assert provider.max_concurrent_requests == 1
    assert provider.supports_concurrency is False


def test_omlx_max_concurrent_requests_enables_supports_concurrency() -> None:
    provider = OmlxProvider("model-a", max_concurrent_requests=8)
    assert provider.max_concurrent_requests == 8
    assert provider.supports_concurrency is True


def test_omlx_same_thread_reuses_session_and_pool_matches_concurrency() -> None:
    provider = OmlxProvider("model-a", max_concurrent_requests=24)
    first = provider._session_for_thread()
    second = provider._session_for_thread()

    assert first is second
    for scheme in ("http://", "https://"):
        adapter = first.get_adapter(scheme)
        assert adapter._pool_connections >= 24
        assert adapter._pool_maxsize >= 24
        assert adapter._pool_block is True


def test_omlx_worker_threads_do_not_share_sessions() -> None:
    provider = OmlxProvider("model-a", max_concurrent_requests=4)
    barrier = threading.Barrier(4)

    def get_twice() -> tuple[requests.Session, requests.Session]:
        first = provider._session_for_thread()
        barrier.wait(timeout=5)
        return first, provider._session_for_thread()

    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        pairs = list(pool.map(lambda _: get_twice(), range(4)))

    assert all(first is second for first, second in pairs)
    assert len({id(first) for first, _ in pairs}) == 4


def test_omlx_consecutive_requests_reuse_same_session(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = OmlxProvider("model-a")
    seen_sessions: list[requests.Session] = []

    def fake_post(session: requests.Session, *_args, **_kwargs):
        seen_sessions.append(session)
        return _response({"choices": [{"message": {"content": "譯文"}}]})

    monkeypatch.setattr(requests.Session, "post", fake_post)
    provider.translate("one", request_id="one")
    provider.translate("two", request_id="two")

    assert len(seen_sessions) == 2
    assert seen_sessions[0] is seen_sessions[1]


@patch("providers.omlx_provider.requests.Session.post")
def test_omlx_translate_temperature_override_does_not_mutate_self(mock_post: MagicMock) -> None:
    """A per-call `temperature` must be used for that request only — mutating
    self.temperature would race when multiple chunks run concurrently on
    separate threads (see translate_book_ollama._translate_chunk)."""
    mock_post.return_value = _response({"choices": [{"message": {"content": "譯文"}}]})

    provider = OmlxProvider("model-a", temperature=0.3)
    provider.translate("prompt", request_id="req", temperature=0.9)

    assert mock_post.call_args.kwargs["json"]["temperature"] == 0.9
    assert provider.temperature == 0.3  # unchanged


@patch("providers.omlx_provider.requests.Session.post")
def test_omlx_translate_without_temperature_override_uses_self(mock_post: MagicMock) -> None:
    mock_post.return_value = _response({"choices": [{"message": {"content": "譯文"}}]})

    provider = OmlxProvider("model-a", temperature=0.3)
    provider.translate("prompt", request_id="req")

    assert mock_post.call_args.kwargs["json"]["temperature"] == 0.3


@patch("providers.omlx_provider.requests.Session.post")
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


@patch("providers.omlx_provider.requests.Session.post")
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


def test_omlx_length_finish_reason_fails_immediately_without_same_temperature_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_post = MagicMock(return_value=_response({
        "choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]
    }))
    monkeypatch.setattr("providers.omlx_provider.requests.Session.post", mock_post)
    with pytest.raises(ProviderError, match="truncated at max_tokens=2048"):
        OmlxProvider("model-a", max_retries=3).translate("prompt", request_id="req")
    assert mock_post.call_count == 1


@patch("providers.omlx_provider.requests.Session.post")
def test_omlx_stop_finish_reason_returns_without_retry(mock_post: MagicMock) -> None:
    mock_post.return_value = _response({
        "choices": [{"message": {"content": "譯文"}, "finish_reason": "stop"}]
    })
    result = OmlxProvider("model-a", max_retries=3).translate("prompt", request_id="req")
    assert result.raw_text == "譯文"
    assert result.metadata["finish_reason"] == "stop"
    assert mock_post.call_count == 1


@patch("providers.omlx_provider.requests.Session.post")
def test_omlx_translate_retries_malformed_response_then_raises(mock_post: MagicMock) -> None:
    mock_post.return_value = _response({"choices": []})

    with pytest.raises(ProviderError):
        OmlxProvider("model-a", max_retries=3).translate("prompt", request_id="req")

    assert mock_post.call_count == 3


@patch("providers.omlx_provider.requests.Session.post")
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


@patch("providers.omlx_provider.requests.Session.get")
def test_omlx_ping_uses_models_endpoint_with_host_override(mock_get: MagicMock) -> None:
    response = MagicMock()
    response.raise_for_status.return_value = None
    mock_get.return_value = response

    assert OmlxProvider("model-a", host="http://127.0.0.1:8099/").ping() is True

    mock_get.assert_called_once_with("http://127.0.0.1:8099/v1/models", timeout=5)


@patch("providers.omlx_provider.requests.Session.get")
def test_omlx_ping_returns_false_on_connection_error(mock_get: MagicMock) -> None:
    mock_get.side_effect = requests.ConnectionError("down")

    assert OmlxProvider("model-a").ping() is False


def test_omlx_api_key_adds_bearer_header_on_ping_and_translate(monkeypatch, tmp_path):
    """雲端 vLLM 用 --api-key 保護公開埠；有金鑰時 ping 與 translate 都要帶 Bearer，沒金鑰時不加 headers。"""
    from unittest.mock import MagicMock, patch

    ok = MagicMock(status_code=200)
    ok.json.return_value = {"choices": [{"message": {"content": "譯文"}}], "usage": {}}
    ok.raise_for_status.return_value = None
    with patch("providers.omlx_provider.requests.Session.get", return_value=ok) as g, \
         patch("providers.omlx_provider.requests.Session.post", return_value=ok) as p:
        prov = OmlxProvider(model="m", host="http://127.0.0.1:8099", api_key="sekret")
        assert prov.ping()
        g.assert_called_once_with("http://127.0.0.1:8099/v1/models", timeout=5,
                                  headers={"Authorization": "Bearer sekret"})
        prov.translate("hi", request_id="r1")
        assert p.call_args.kwargs["headers"] == {"Authorization": "Bearer sekret"}
    with patch("providers.omlx_provider.requests.Session.get", return_value=ok) as g:
        OmlxProvider(model="m", host="http://127.0.0.1:8099").ping()
        assert "headers" not in g.call_args.kwargs

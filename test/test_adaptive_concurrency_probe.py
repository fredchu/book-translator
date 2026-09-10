from __future__ import annotations

import http.server
import importlib.util
import json
from pathlib import Path
import threading
import time
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO / "scripts" / "adaptive_concurrency_probe.py"
spec = importlib.util.spec_from_file_location("adaptive_concurrency_probe", MODULE_PATH)
assert spec and spec.loader
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def _requests() -> list[dict[str, str]]:
    return [{"system": "system", "user": f"large unique chunk {i}"} for i in range(60)]


def _fake_wave(
    metrics: dict[int, tuple[float, float]], seen: list[tuple[int, list[str]]]
):
    def run(batch: list[dict[str, Any]], n: int) -> dict[str, Any]:
        seen.append((n, [row["user"] for row in batch]))
        speed, latency = metrics[n]
        return {"n": n, "requests": n, "mean_single_tok_s": speed, "max_latency_s": latency}

    return run


@pytest.mark.parametrize(
    ("expected", "metrics"),
    [
        (24, {24: (22.0, 20.0)}),
        (16, {24: (21.0, 20.0), 16: (22.0, 20.0)}),
        (12, {24: (21.0, 20.0), 16: (22.0, 60.0), 12: (22.0, 20.0)}),
        (8, {24: (21.0, 20.0), 16: (21.0, 20.0), 12: (21.0, 20.0), 8: (22.0, 20.0)}),
    ],
)
def test_selects_each_level_and_never_reuses_prompts(
    expected: int, metrics: dict[int, tuple[float, float]]
) -> None:
    seen: list[tuple[int, list[str]]] = []
    selected, waves, passed = probe.choose_concurrency(_requests(), _fake_wave(metrics, seen))
    assert selected == expected and passed is True
    assert [wave["n"] for wave in waves] == [n for n in probe.CANDIDATES if n >= expected]
    used = [prompt for _, prompts in seen for prompt in prompts]
    assert len(used) == len(set(used))


def test_all_levels_fail_returns_eight_and_warning_signal() -> None:
    seen: list[tuple[int, list[str]]] = []
    metrics = {n: (21.0, 20.0) for n in probe.CANDIDATES}
    selected, waves, passed = probe.choose_concurrency(_requests(), _fake_wave(metrics, seen))
    assert selected == 8
    assert passed is False
    assert len(waves) == 4


def test_one_slow_request_rejects_wave_even_when_average_passes() -> None:
    seen: list[tuple[int, list[str]]] = []
    metrics = {24: (30.0, 60.0), 16: (22.0, 20.0)}
    selected, waves, _ = probe.choose_concurrency(_requests(), _fake_wave(metrics, seen))
    assert selected == 16
    assert waves[0]["passed"] is False


def test_speed_threshold_is_derived_from_max_tokens_and_timeout() -> None:
    seen: list[tuple[int, list[str]]] = []
    metrics = {24: (30.0, 10.0), 16: (50.0, 10.0)}
    selected, waves, _ = probe.choose_concurrency(
        _requests(), _fake_wave(metrics, seen), max_tokens=4096, timeout=120
    )
    assert waves[0]["speed_threshold_tok_s"] == pytest.approx(42.667, abs=0.001)
    assert selected == 16


def test_long_book_load_uses_sixty_different_large_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    builder_path = REPO / "scripts" / "build_adaptive_probe_load.py"
    builder_spec = importlib.util.spec_from_file_location("build_adaptive_probe_load_long", builder_path)
    assert builder_spec and builder_spec.loader
    builder = importlib.util.module_from_spec(builder_spec)
    builder_spec.loader.exec_module(builder)
    rows = [
        {"kind": "chunk", "system": "s", "user": f"u{i}", "source_chars": 3000}
        for i in range(70)
    ]
    monkeypatch.setattr(builder, "prompts_from_epub", lambda _path: rows)
    load = builder.build_load(Path("book.epub"))
    assert load["repeated_for_short_book"] is False
    assert len({row["user"] for row in load["requests"]}) == 60


def test_short_book_load_records_unavoidable_repetition() -> None:
    builder_path = REPO / "scripts" / "build_adaptive_probe_load.py"
    builder_spec = importlib.util.spec_from_file_location("build_adaptive_probe_load", builder_path)
    assert builder_spec and builder_spec.loader
    builder = importlib.util.module_from_spec(builder_spec)
    builder_spec.loader.exec_module(builder)

    load = builder.build_load(REPO / "test" / "fixtures" / "full_structure.epub")
    assert len(load["requests"]) == 60
    assert load["repeated_for_short_book"] is True
    assert load["available_chunks"] > 0
    assert all(row["kind"] == "chunk" for row in load["requests"])


def test_real_http_wave_measures_completion_tokens_latency_and_parallelism() -> None:
    class Handler(http.server.BaseHTTPRequestHandler):
        active = 0
        max_active = 0
        lock = threading.Lock()

        def do_POST(self):  # noqa: N802
            size = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(size))
            user = body["messages"][1]["content"]
            with self.lock:
                self.__class__.active += 1
                self.__class__.max_active = max(self.__class__.max_active, self.__class__.active)
            time.sleep(0.08 if user.endswith("3") else 0.02)
            with self.lock:
                self.__class__.active -= 1
            payload = json.dumps({
                "choices": [{"message": {"content": "譯文"}}],
                "usage": {"completion_tokens": 4},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        rows = [{"system": "s", "user": f"chunk-{i}"} for i in range(4)]
        wave = probe.probe_wave(
            f"http://127.0.0.1:{server.server_address[1]}", "model", "key", rows, 4, 2048, 2
        )
        assert Handler.max_active == 4
        assert wave["max_latency_s"] >= 0.07
        assert wave["mean_single_tok_s"] > 40
    finally:
        server.shutdown()


def test_requires_sixty_distinct_large_probe_slots() -> None:
    with pytest.raises(ValueError, match="needs 60 distinct large chunks"):
        probe.choose_concurrency(_requests()[:59], lambda _batch, _n: {})

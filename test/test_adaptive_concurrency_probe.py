from __future__ import annotations

import http.server
import importlib.util
import json
from pathlib import Path
import statistics
import threading
from typing import Any

import pytest
import requests

REPO = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO / "scripts" / "adaptive_concurrency_probe.py"
spec = importlib.util.spec_from_file_location("adaptive_concurrency_probe", MODULE_PATH)
assert spec and spec.loader
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def _requests(n: int = 200) -> list[dict[str, str]]:
    return [{"system": "system", "user": f"large unique chunk {i}"} for i in range(n)]


def _fake_wave(
    metrics: dict[int, dict[str, float]], seen: list[tuple[int, list[str]]]
):
    """metrics[n] = {mean_single_tok_s, aggregate_tok_per_s, max_latency_s, median_latency_s}.

    mean_single_tok_s (per-request speed) and aggregate_tok_per_s (whole-wave
    throughput) are kept independent here on purpose: in reality the former
    falls as N rises even while the latter climbs, and fixtures need that
    realistic shape or they'd never catch a gate that gets this backwards
    (see test_saturation_gate_uses_aggregate_throughput_not_per_request_speed)."""

    def run(batch: list[dict[str, Any]], n: int) -> dict[str, Any]:
        seen.append((n, [row["user"] for row in batch]))
        m = metrics[n]
        return {
            "n": n,
            "requests": n,
            "mean_single_tok_s": m["mean_single_tok_s"],
            "aggregate_tok_per_s": m["aggregate_tok_per_s"],
            "max_latency_s": m["max_latency_s"],
            "median_latency_s": m["median_latency_s"],
        }

    return run


def test_requires_full_candidate_pool_distinct_chunks() -> None:
    required = sum(probe.CANDIDATES)
    with pytest.raises(ValueError, match=f"needs {required} distinct large chunks"):
        probe.choose_concurrency(_requests(required - 1), lambda _batch, _n: {})


def _m(mean_single: float, aggregate: float, max_latency: float, median_latency: float) -> dict[str, float]:
    return {
        "mean_single_tok_s": mean_single,
        "aggregate_tok_per_s": aggregate,
        "max_latency_s": max_latency,
        "median_latency_s": median_latency,
    }


@pytest.mark.parametrize(
    ("expected", "waves_run", "metrics"),
    [
        # Realistic shape throughout: per-request speed FALLS every step (more
        # contention at higher N) while aggregate throughput keeps climbing
        # >=10% and latency stays within 3x its own median -> climb to the top.
        (32, 5, {8: _m(32.0, 240.0, 10.0, 10.0), 12: _m(28.0, 280.0, 10.0, 10.0),
                 16: _m(24.0, 320.0, 10.0, 10.0), 24: _m(21.0, 370.0, 10.0, 10.0),
                 32: _m(19.0, 430.0, 10.0, 10.0)}),
        # 16->24 aggregate gains only ~4.7% (<10%) -> stop at 16, but 24 is
        # still RUN to detect that. Per-request speed keeps falling the whole
        # way, same as real hardware — it must not be what the gate reads.
        (16, 4, {8: _m(32.0, 240.0, 10.0, 10.0), 12: _m(28.0, 280.0, 10.0, 10.0),
                 16: _m(24.0, 320.0, 10.0, 10.0), 24: _m(22.0, 335.0, 10.0, 10.0)}),
        # 12 backs up (max >> median) -> fall back to 8, the last confirmed-safe tier.
        (8, 2, {8: _m(32.0, 240.0, 10.0, 10.0), 12: _m(28.0, 260.0, 40.0, 10.0)}),
    ],
)
def test_climbs_while_saturating_and_stops_on_first_failed_gate(
    expected: int, waves_run: int, metrics: dict[int, dict[str, float]]
) -> None:
    seen: list[tuple[int, list[str]]] = []
    selected, waves, passed, _timeout = probe.choose_concurrency(
        _requests(), _fake_wave(metrics, seen)
    )
    assert selected == expected
    assert passed is True
    assert len(waves) == waves_run
    used = [prompt for _, prompts in seen for prompt in prompts]
    assert len(used) == len(set(used))  # every wave got disjoint prompts


def test_smallest_candidate_backing_up_returns_it_with_passed_false() -> None:
    seen: list[tuple[int, list[str]]] = []
    metrics = {8: _m(32.0, 240.0, 40.0, 10.0)}  # 40 > 3*10 -> backs up with no prior tier to fall back to
    selected, waves, passed, _timeout = probe.choose_concurrency(
        _requests(), _fake_wave(metrics, seen)
    )
    assert selected == 8
    assert passed is False
    assert len(waves) == 1  # never climbs past a failing first tier


def test_backlog_gate_rejects_a_straggler_even_though_mean_speed_is_fine() -> None:
    """A single stuck request can inflate max_latency far past the median while
    barely moving throughput — the backlog gate must still catch it."""
    seen: list[tuple[int, list[str]]] = []
    # throughput barely changed (would pass saturation on its own), latency backed way up
    metrics = {8: _m(32.0, 240.0, 10.0, 10.0), 12: _m(28.0, 245.0, 35.0, 10.0)}
    selected, waves, passed, _timeout = probe.choose_concurrency(
        _requests(), _fake_wave(metrics, seen)
    )
    assert selected == 8
    assert passed is True
    assert waves[1]["backlog_ok"] is False


def test_saturation_gate_uses_aggregate_throughput_not_per_request_speed() -> None:
    """Per-request speed naturally FALLS as concurrency rises (more requests
    sharing the same GPU/scheduler), even while the server does strictly more
    total work. A gate that compared mean_single_tok_s across tiers would see
    a negative "gain" on the very first climb and freeze at the smallest
    candidate forever, no matter how much real headroom the server has. The
    gate must read aggregate (whole-wave) throughput instead."""
    seen: list[tuple[int, list[str]]] = []
    metrics = {
        8: _m(32.0, 240.0, 10.0, 10.0),
        # per-request DOWN 12.5% (32->28), aggregate UP 25% (240->300) -> climbs
        12: _m(28.0, 300.0, 10.0, 10.0),
        # per-request DOWN further (28->24), aggregate still UP 13.3% -> climbs
        16: _m(24.0, 340.0, 10.0, 10.0),
        # per-request DOWN further again, aggregate gain only ~5.9% (<10%) -> stop at 16
        24: _m(20.0, 360.0, 10.0, 10.0),
    }
    selected, waves, passed, _timeout = probe.choose_concurrency(
        _requests(), _fake_wave(metrics, seen)
    )
    assert selected == 16
    assert passed is True
    assert len(waves) == 4
    # Sanity: per-request speed fell at every single step in this fixture —
    # if the gate were reading mean_single_tok_s it could never have climbed
    # past N=8 at all.
    speeds = [w["mean_single_tok_s"] for w in waves]
    assert speeds == sorted(speeds, reverse=True)


def test_derive_timeout_uses_the_larger_of_two_bounds() -> None:
    """spec-05: the derived timeout is computed from the SELECTED wave's own
    measurements, not a fixed constant — assert the formula, never a literal
    like 180 (that number came from one specific measurement run and is not
    stable across machines/profiles)."""
    wave = {"max_latency_s": 20.0, "mean_single_tok_s": 10.0}
    max_tokens = 2048
    result = probe.derive_timeout(max_tokens, wave)
    from_latency = probe.LATENCY_MULTIPLE * wave["max_latency_s"]
    from_speed = max_tokens / wave["mean_single_tok_s"] / probe.TIMEOUT_HEADROOM
    assert result == max(from_latency, from_speed)
    # sanity: this fixture's numbers make the speed-derived bound the larger one
    assert from_speed > from_latency
    assert result == pytest.approx(256.0, rel=1e-9)


def test_derive_timeout_latency_bound_can_dominate() -> None:
    wave = {"max_latency_s": 60.0, "mean_single_tok_s": 200.0}
    max_tokens = 2048
    result = probe.derive_timeout(max_tokens, wave)
    from_latency = probe.LATENCY_MULTIPLE * wave["max_latency_s"]
    assert result == from_latency
    assert result == pytest.approx(180.0, rel=1e-9)  # coincidence of this fixture's numbers, not asserted elsewhere


def test_choose_concurrency_returns_timeout_derived_from_selected_tier() -> None:
    """Single source in practice: the timeout that comes back is computed
    from the SAME wave whose N got selected, using the real derive_timeout()
    formula — not a separate hardcoded value."""
    seen: list[tuple[int, list[str]]] = []
    # 12 climbs (aggregate +16.7% over 8); 16's aggregate gain (~5.4%) fails
    # saturation -> selection stops at 12, whose OWN wave feeds derive_timeout().
    metrics = {8: _m(32.0, 240.0, 10.0, 10.0), 12: _m(28.0, 280.0, 12.0, 10.0),
               16: _m(24.0, 295.0, 14.0, 12.0)}
    selected, waves, _passed, timeout = probe.choose_concurrency(
        _requests(), _fake_wave(metrics, seen), max_tokens=4096
    )
    selected_wave = next(w for w in waves if w["n"] == selected)
    assert timeout == probe.derive_timeout(4096, selected_wave)


def test_long_book_load_uses_all_required_distinct_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    builder_path = REPO / "scripts" / "build_adaptive_probe_load.py"
    builder_spec = importlib.util.spec_from_file_location("build_adaptive_probe_load_long", builder_path)
    assert builder_spec and builder_spec.loader
    builder = importlib.util.module_from_spec(builder_spec)
    builder_spec.loader.exec_module(builder)
    required = builder.REQUIRED
    rows = [
        {"kind": "chunk", "system": "s", "user": f"u{i}", "source_chars": 3000}
        for i in range(required + 10)
    ]
    monkeypatch.setattr(builder, "prompts_from_epub", lambda _path: rows)
    load = builder.build_load(Path("book.epub"))
    assert load["repeated_for_short_book"] is False
    assert len({row["user"] for row in load["requests"]}) == required


def test_short_book_load_records_unavoidable_repetition() -> None:
    builder_path = REPO / "scripts" / "build_adaptive_probe_load.py"
    builder_spec = importlib.util.spec_from_file_location("build_adaptive_probe_load", builder_path)
    assert builder_spec and builder_spec.loader
    builder = importlib.util.module_from_spec(builder_spec)
    builder_spec.loader.exec_module(builder)

    load = builder.build_load(REPO / "test" / "fixtures" / "full_structure.epub")
    assert len(load["requests"]) == builder.REQUIRED
    assert load["repeated_for_short_book"] is True
    assert load["available_chunks"] > 0
    assert all(row["kind"] == "chunk" for row in load["requests"])


def test_build_adaptive_probe_load_required_matches_probe_candidates() -> None:
    """The two files must not each hardcode their own copy of this number —
    that's the exact shape of bug spec-05 fixed for timeout/max_tokens."""
    builder_path = REPO / "scripts" / "build_adaptive_probe_load.py"
    builder_spec = importlib.util.spec_from_file_location("build_adaptive_probe_load_check", builder_path)
    assert builder_spec and builder_spec.loader
    builder = importlib.util.module_from_spec(builder_spec)
    builder_spec.loader.exec_module(builder)
    assert builder.REQUIRED == sum(probe.CANDIDATES)


def test_real_http_wave_measures_completion_tokens_latency_and_parallelism() -> None:
    class Handler(http.server.BaseHTTPRequestHandler):
        active = 0
        max_active = 0
        lock = threading.Lock()
        all_requests_arrived = threading.Barrier(4)

        def do_POST(self):  # noqa: N802
            size = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(size))
            assert body["messages"][1]["content"].startswith("chunk-")
            with self.lock:
                self.__class__.active += 1
                self.__class__.max_active = max(self.__class__.max_active, self.__class__.active)
            # Deterministic overlap: no response may leave until all four handlers
            # have arrived. This tests real parallel requests without scheduler timing.
            self.__class__.all_requests_arrived.wait(timeout=5)
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
        samples = wave["samples"]
        assert all(row["out_tokens"] == 4 for row in samples)
        expected_speed = statistics.mean(
            row["out_tokens"] / row["elapsed_s"] for row in samples
        )
        assert wave["mean_single_tok_s"] == pytest.approx(expected_speed, rel=1e-9)
        assert wave["max_latency_s"] == max(row["elapsed_s"] for row in samples)
        assert wave["median_latency_s"] == statistics.median(row["elapsed_s"] for row in samples)
        expected_aggregate = sum(row["out_tokens"] for row in samples) / wave["max_latency_s"]
        assert wave["aggregate_tok_per_s"] == pytest.approx(expected_aggregate, rel=1e-9)
    finally:
        server.shutdown()


def _evidence_from_bodies(steps: list[tuple[str, str, bool]]):
    """steps: (server_info_body, metrics_body, alive) consumed in call order
    (one call per fetch_evidence() invocation — before/after each wave, and
    again after any failed wave for the liveness check). Once exhausted,
    repeats the last step — most tests only care about the first few calls."""
    remaining = list(steps)

    def fetch() -> dict[str, Any]:
        si_body, m_body, alive = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        entry = lambda body: {  # noqa: E731
            "alive": alive,
            "status_code": 200 if alive else None,
            "body": body,
            "error": None if alive else "connection refused",
        }
        return {"server_info": entry(si_body), "metrics": entry(m_body)}

    return fetch


def test_retract_signal_sums_numbers_on_any_line_mentioning_retract() -> None:
    """Field/endpoint name is deliberately not pinned (spec-07 review) — any
    line containing 'retract' anywhere in either endpoint's raw text counts."""
    snapshot = {
        "server_info": {"alive": True, "body": "num_retracted_reqs: 3\nqueue_len: 99"},
        "metrics": {"alive": True, "body": 'sglang:num_retract_total{gpu="0"} 7'},
    }
    assert probe.retract_signal(snapshot) == 10.0


def test_retract_signal_returns_none_not_zero_when_nothing_matches() -> None:
    """None (unknown), not 0 (confirmed no retracts) — the caller must fall
    back to the latency gate rather than trust an endpoint that may not even
    expose this counter on this SGLang build."""
    snapshot = {
        "server_info": {"alive": True, "body": "queue_len: 3"},
        "metrics": {"alive": True, "body": ""},
    }
    assert probe.retract_signal(snapshot) is None


def test_backlog_gate_prefers_retract_signal_over_latency() -> None:
    """The retract signal must OVERRIDE a latency reading that would have
    passed on its own — this is the whole point of preferring a direct
    signal over a proxy (spec-07)."""
    seen: list[tuple[int, list[str]]] = []
    metrics = {8: _m(32.0, 240.0, 10.0, 10.0), 12: _m(28.0, 280.0, 10.0, 10.0)}
    evidence = _evidence_from_bodies([
        ("num_retracted: 0", "", True),  # before N=8
        ("num_retracted: 0", "", True),  # after N=8
        ("num_retracted: 0", "", True),  # before N=12
        ("num_retracted: 3", "", True),  # after N=12 -- retract count rose during the wave
    ])
    selected, waves, passed, _timeout = probe.choose_concurrency(
        _requests(), _fake_wave(metrics, seen), fetch_evidence=evidence
    )
    assert selected == 8
    assert passed is True
    assert waves[1]["backlog_gate_used"] == "retract"
    assert waves[1]["backlog_ok"] is False
    assert waves[1]["retract_before"] == 0.0
    assert waves[1]["retract_after"] == 3.0


def test_backlog_gate_falls_back_to_latency_when_no_retract_signal_anywhere() -> None:
    seen: list[tuple[int, list[str]]] = []
    # Full candidate set, all latencies flat at 10.0 (well within any multiple
    # of their own median) so every tier's backlog gate passes purely on the
    # latency fallback -- and aggregate throughput keeps climbing >=10% so
    # saturation never stops it early either, all the way to the top.
    metrics = {8: _m(32.0, 240.0, 10.0, 10.0), 12: _m(28.0, 280.0, 10.0, 10.0),
               16: _m(24.0, 320.0, 10.0, 10.0), 24: _m(21.0, 370.0, 10.0, 10.0),
               32: _m(19.0, 430.0, 10.0, 10.0)}
    selected, waves, passed, _timeout = probe.choose_concurrency(
        _requests(), _fake_wave(metrics, seen)  # default fetch_evidence -> empty bodies, no signal
    )
    assert selected == 32
    assert passed is True
    assert all(w["backlog_gate_used"] == "latency_fallback" for w in waves)


def test_backlog_latency_fallback_tightened_to_2x_not_3x() -> None:
    """A real observed straggler on SGLang fp8 hit 2.44x its wave's own
    median. The old shared 3.0x constant would have let it through; the
    fallback must now be 2.0x. 25.0/10.0 = 2.5x: fails at 2x, would have
    passed at the old 3x."""
    seen: list[tuple[int, list[str]]] = []
    metrics = {8: _m(32.0, 240.0, 10.0, 10.0), 12: _m(28.0, 280.0, 25.0, 10.0)}
    selected, waves, passed, _timeout = probe.choose_concurrency(
        _requests(), _fake_wave(metrics, seen)
    )
    assert selected == 8
    assert passed is True
    assert waves[1]["backlog_gate_used"] == "latency_fallback"
    assert waves[1]["backlog_ok"] is False


def test_wave_failure_with_server_alive_retreats_to_last_good_tier_normally() -> None:
    """spec-07 discrimination table row C: a wave failing while the server
    still answers is a normal early stop, not an error — must return
    normally (passed=True), not raise."""
    seen: list[tuple[int, list[str]]] = []
    metrics = {8: _m(32.0, 240.0, 10.0, 10.0)}

    def run(batch: list[dict[str, Any]], n: int) -> dict[str, Any]:
        if n == 8:
            return _fake_wave(metrics, seen)(batch, n)
        raise requests.HTTPError("500 Server Error")

    evidence = _evidence_from_bodies([("", "", True)])
    selected, waves, passed, timeout = probe.choose_concurrency(
        _requests(), run, fetch_evidence=evidence
    )
    assert selected == 8
    assert passed is True
    assert waves[-1]["request_failed"] is True
    assert waves[-1]["server_alive_after_failure"] is True
    assert timeout == probe.derive_timeout(probe.DEFAULT_MAX_TOKENS, waves[0])


def test_wave_failure_at_smallest_candidate_with_server_alive_raises_plain_error() -> None:
    """No prior successful wave exists to retreat to, yet the server is
    alive -- this must NOT be reported as ServerDiedError (it isn't dead)."""

    def run(_batch: list[dict[str, Any]], _n: int) -> dict[str, Any]:
        raise requests.HTTPError("500 Server Error")

    evidence = _evidence_from_bodies([("", "", True)])
    with pytest.raises(RuntimeError) as exc_info:
        probe.choose_concurrency(_requests(), run, fetch_evidence=evidence)
    assert not isinstance(exc_info.value, probe.ServerDiedError)


def test_wave_failure_with_no_liveness_response_raises_server_died_error() -> None:
    """spec-07 discrimination table row E: no response from either
    diagnostic endpoint means the process is gone -- must raise
    ServerDiedError specifically, carrying the evidence gathered so far."""

    def run(_batch: list[dict[str, Any]], _n: int) -> dict[str, Any]:
        raise ConnectionError("Connection refused")

    evidence = _evidence_from_bodies([("", "", False)])
    with pytest.raises(probe.ServerDiedError) as exc_info:
        probe.choose_concurrency(_requests(), run, fetch_evidence=evidence)
    assert exc_info.value.waves[-1]["server_alive_after_failure"] is False
    assert exc_info.value.waves[-1]["request_failed"] is True


def test_wave_failure_after_partial_success_with_server_dead_keeps_prior_waves() -> None:
    seen: list[tuple[int, list[str]]] = []
    metrics = {8: _m(32.0, 240.0, 10.0, 10.0), 12: _m(28.0, 280.0, 10.0, 10.0)}

    def run(batch: list[dict[str, Any]], n: int) -> dict[str, Any]:
        if n in metrics:
            return _fake_wave(metrics, seen)(batch, n)
        raise ConnectionError("Connection refused")

    # Alive through N=8 and N=12 (2 calls each); dies exactly when checked
    # after N=16's request fails (5th call).
    evidence = _evidence_from_bodies([("", "", True)] * 5 + [("", "", False)])
    with pytest.raises(probe.ServerDiedError) as exc_info:
        probe.choose_concurrency(_requests(), run, fetch_evidence=evidence)
    assert len(exc_info.value.waves) == 3
    assert exc_info.value.waves[0]["n"] == 8 and exc_info.value.waves[0].get("request_failed") is None
    assert exc_info.value.waves[1]["n"] == 12 and exc_info.value.waves[1].get("request_failed") is None
    assert exc_info.value.waves[2]["n"] == 16 and exc_info.value.waves[2]["request_failed"] is True


def test_probe_one_http_error_includes_response_body_not_just_status() -> None:
    """spec-07 item (c): raise_for_status() alone discards the body, and
    SGLang puts the real failure reason there."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            # Drain the request body before responding -- an unread body
            # still sitting in the socket's receive buffer at close time can
            # trigger a TCP RST instead of a clean FIN (observed as
            # intermittent ChunkedEncodingError/"Connection reset by peer"
            # under full-suite load). Every other handler in this file reads
            # its body for this reason; this one skipped it since it doesn't
            # need the content, which is exactly what made it flaky.
            size = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(size)
            body = b'{"error": "linear_attn assert failed: seq_lens mismatch"}'
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(requests.HTTPError) as exc_info:
            probe.probe_one(
                f"http://127.0.0.1:{server.server_address[1]}", "model", "key",
                {"system": "s", "user": "u"}, 2048, 5, threading.Barrier(1),
            )
        assert "linear_attn assert failed" in str(exc_info.value)
    finally:
        server.shutdown()


def test_fetch_endpoint_text_alive_true_on_any_status_code() -> None:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"oom")

        def log_message(self, _format, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = probe.fetch_endpoint_text(f"http://127.0.0.1:{server.server_address[1]}/x", "")
        assert result["alive"] is True
        assert result["status_code"] == 500
        assert result["body"] == "oom"
    finally:
        server.shutdown()


def test_fetch_endpoint_text_alive_false_on_connection_refused() -> None:
    # Nothing listens on this port -- a real connection-level failure, not a status code.
    result = probe.fetch_endpoint_text("http://127.0.0.1:1", "", timeout=1.0)
    assert result["alive"] is False
    assert result["body"] == ""


def test_probe_own_default_max_tokens_matches_translator_single_source() -> None:
    """The probe used to hardcode its own DEFAULT_MAX_TOKENS = 2048, a
    separate copy of the same number the translator resolves via
    providers.omlx_provider.DEFAULT_MAX_TOKENS — the exact duplication
    pattern spec-05 exists to remove."""
    import sys
    sys.path.insert(0, str(REPO / "scripts"))
    from providers.omlx_provider import DEFAULT_MAX_TOKENS as translator_default
    assert probe.DEFAULT_MAX_TOKENS is translator_default

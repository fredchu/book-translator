from __future__ import annotations

import http.server
import importlib.util
import json
from pathlib import Path
import statistics
import threading
from typing import Any

import pytest

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


def test_probe_own_default_max_tokens_matches_translator_single_source() -> None:
    """The probe used to hardcode its own DEFAULT_MAX_TOKENS = 2048, a
    separate copy of the same number the translator resolves via
    providers.omlx_provider.DEFAULT_MAX_TOKENS — the exact duplication
    pattern spec-05 exists to remove."""
    import sys
    sys.path.insert(0, str(REPO / "scripts"))
    from providers.omlx_provider import DEFAULT_MAX_TOKENS as translator_default
    assert probe.DEFAULT_MAX_TOKENS is translator_default

from __future__ import annotations

import hashlib
import http.server
import importlib.util
import json
from pathlib import Path
import statistics
import threading
import time
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
    """metrics[n] = {mean_single_tok_s, steady_state_tok_per_s, aggregate_tok_per_s,
    max_latency_s, median_latency_s} (see _m()).

    steady_state_tok_per_s is the PRIMARY metric (2026-09-10 second redesign)
    that choose_tier()/choose_concurrency() actually compare across tiers.
    aggregate_tok_per_s and mean_single_tok_s are kept independent so
    fixtures can prove the gate reads the right one (see
    test_saturation_gate_reads_steady_state_not_aggregate_or_mean_single)."""

    def run(batch: list[dict[str, Any]], n: int) -> dict[str, Any]:
        seen.append((n, [row["user"] for row in batch]))
        m = metrics[n]
        return {
            "n": n,
            "requests": n,
            "mean_single_tok_s": m["mean_single_tok_s"],
            "steady_state_tok_per_s": m["steady_state_tok_per_s"],
            "aggregate_tok_per_s": m["aggregate_tok_per_s"],
            "max_latency_s": m["max_latency_s"],
            "median_latency_s": m["median_latency_s"],
        }

    return run


def test_requires_full_candidate_pool_distinct_chunks() -> None:
    required = len(probe.CANDIDATES)
    with pytest.raises(ValueError, match=f"needs at least {required} distinct prompts"):
        probe.choose_concurrency(_requests(required - 1), lambda _batch, _n: {})


def _m(
    mean_single: float,
    steady: float,
    max_latency: float,
    median_latency: float,
    *,
    aggregate: float | None = None,
) -> dict[str, float]:
    """aggregate defaults to steady when not given -- most fixtures don't
    care about the (now reference-only) old metric and shouldn't need to
    invent a plausible-looking value for it."""
    return {
        "mean_single_tok_s": mean_single,
        "steady_state_tok_per_s": steady,
        "aggregate_tok_per_s": aggregate if aggregate is not None else steady,
        "max_latency_s": max_latency,
        "median_latency_s": median_latency,
    }


@pytest.mark.parametrize(
    ("expected", "waves_run", "metrics"),
    [
        # Monotonic climb: every tier is a new best -> climb to the top.
        (32, 5, {8: _m(32.0, 240.0, 10.0, 10.0), 12: _m(28.0, 280.0, 10.0, 10.0),
                 16: _m(24.0, 320.0, 10.0, 10.0), 24: _m(21.0, 370.0, 10.0, 10.0),
                 32: _m(19.0, 430.0, 10.0, 10.0)}),
        # 16 dips slightly below the best (280) -- within the 5% stopping
        # tolerance (not "is_low") but ALSO too small a gain over 12 to clear
        # the marginal-efficiency bar for a new best, so it's simply a
        # plateau: best stays 12. 24 then posts a real, big-enough gain to
        # clear the (now steeper, since it's jumping from N=12) efficiency
        # bar and retakes best; 32 does the same again from 24. This is the
        # shape that burned real hardware: an early-stop-on-first-non-gain
        # design would have frozen at 12, missing that 32 goes on to be the
        # real best -- recovering past a stale anchor is still possible
        # here, it just needs a big enough real gain, not just any positive one.
        (32, 5, {8: _m(32.0, 240.0, 10.0, 10.0), 12: _m(28.0, 280.0, 10.0, 10.0),
                 16: _m(24.0, 270.0, 10.0, 10.0), 24: _m(21.0, 380.0, 10.0, 10.0),
                 32: _m(19.0, 430.0, 10.0, 10.0)}),
        # 16 AND 24 both stay >5% below the best (280) -- two consecutive
        # misses that never recover -> stop after 24, select 12 (the best
        # measured), never even try 32.
        (12, 4, {8: _m(32.0, 240.0, 10.0, 10.0), 12: _m(28.0, 280.0, 10.0, 10.0),
                 16: _m(24.0, 260.0, 10.0, 10.0), 24: _m(21.0, 255.0, 10.0, 10.0)}),
        # 12 backs up (max >> median) -> fall back to 8, the last confirmed-safe tier.
        # Backlog gate stopping the climb immediately is UNCHANGED by this redesign.
        (8, 2, {8: _m(32.0, 240.0, 10.0, 10.0), 12: _m(28.0, 260.0, 40.0, 10.0)}),
    ],
)
def test_asymmetric_gate_climbs_and_selects_best_not_highest_or_last(
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
    assert len(used) == len(set(used))  # every tier got disjoint prompts


def test_pool_split_is_proportional_to_each_tiers_own_n_not_uniform() -> None:
    """A bigger tier's closed loop can burn through far more prompts in the
    same T seconds than a smaller tier's -- an even split would starve N=32
    of distinct prompts while leaving N=8 with far more than it needs. The
    slice sizes must scale with each candidate's own N (rounded), not be
    equal-sized."""
    seen: list[tuple[int, list[str]]] = []
    metrics = {8: _m(32.0, 240.0, 10.0, 10.0), 12: _m(28.0, 280.0, 10.0, 10.0),
               16: _m(24.0, 320.0, 10.0, 10.0), 24: _m(21.0, 370.0, 10.0, 10.0),
               32: _m(19.0, 430.0, 10.0, 10.0)}
    total = probe.REQUIRED
    selected, waves, passed, _timeout = probe.choose_concurrency(
        _requests(total), _fake_wave(metrics, seen)
    )
    assert selected == 32 and passed is True
    shares = {n: len(prompts) for n, prompts in seen}
    assert sum(shares.values()) == total  # the whole pool is consumed, no prompts dropped
    # Sizes strictly increase with N, and roughly track each tier's own N
    # (allowing for rounding): an even split would instead give every tier
    # about total/len(candidates) == 46 prompts regardless of N.
    ordered_ns = sorted(shares)
    assert ordered_ns == list(probe.CANDIDATES)
    assert [shares[n] for n in ordered_ns] == sorted(shares.values())  # strictly increasing
    total_weight = sum(probe.CANDIDATES)
    for n in probe.CANDIDATES[:-1]:  # last tier absorbs rounding, checked via the sum() above
        expected = round(total * n / total_weight)
        assert shares[n] == pytest.approx(expected, abs=1)


def test_smallest_candidate_backing_up_returns_it_with_passed_false() -> None:
    seen: list[tuple[int, list[str]]] = []
    metrics = {8: _m(32.0, 240.0, 40.0, 10.0)}  # 40 > 3*10 -> backs up with no prior tier to fall back to
    selected, waves, passed, _timeout = probe.choose_concurrency(
        _requests(), _fake_wave(metrics, seen)
    )
    assert selected == 8
    assert passed is False
    assert len(waves) == 1  # never climbs past a failing first tier


def test_backlog_gate_rejects_a_straggler_even_though_throughput_is_fine() -> None:
    """A single stuck request can inflate max_latency far past the median while
    barely moving throughput — the backlog gate must still catch it."""
    seen: list[tuple[int, list[str]]] = []
    # throughput barely changed (would pass the saturation gate on its own), latency backed way up
    metrics = {8: _m(32.0, 240.0, 10.0, 10.0), 12: _m(28.0, 245.0, 35.0, 10.0)}
    selected, waves, passed, _timeout = probe.choose_concurrency(
        _requests(), _fake_wave(metrics, seen)
    )
    assert selected == 8
    assert passed is True
    assert waves[1]["backlog_ok"] is False


def test_saturation_gate_reads_steady_state_not_aggregate_or_mean_single() -> None:
    """2026-09-10 second redesign: the gate now compares steady_state_tok_per_s
    across tiers, not aggregate_tok_per_s (the first redesign's metric) and
    never mean_single_tok_s. Construct tiers where aggregate/mean_single look
    like a huge improvement while steady_state is actually a sustained dip --
    if the gate read the wrong field, it would keep climbing; reading the
    right one, it must stop at N=8 (the only tier with a good steady_state
    value) without ever being tempted by the other two fields' shape."""
    seen: list[tuple[int, list[str]]] = []
    metrics = {
        8: _m(10.0, 100.0, 10.0, 10.0, aggregate=50.0),
        # steady_state DOWN 10% (below the 95% tolerance) -- a real dip --
        # while mean_single_tok_s and aggregate_tok_per_s both look dramatically
        # BETTER than tier 8's, which a buggy gate reading either would chase.
        12: _m(50.0, 90.0, 10.0, 10.0, aggregate=200.0),
        16: _m(80.0, 85.0, 10.0, 10.0, aggregate=400.0),
    }
    selected, waves, passed, _timeout = probe.choose_concurrency(
        _requests(), _fake_wave(metrics, seen)
    )
    assert selected == 8
    assert passed is True
    assert len(waves) == 3  # stopped after 16 (two consecutive misses vs the N=8 best)
    # Sanity: per-request speed and aggregate both climb sharply in this
    # fixture — if the gate were reading either instead of steady_state, it
    # would have kept climbing past N=8, not stopped there.
    speeds = [w["mean_single_tok_s"] for w in waves]
    assert speeds == sorted(speeds)
    aggregates = [w["aggregate_tok_per_s"] for w in waves]
    assert aggregates == sorted(aggregates)


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
    # 12 is a new best over 8; 16 and 24 both dip >5% below 12's best and never
    # recover -> stop after 24, select 12, whose OWN wave feeds derive_timeout().
    metrics = {8: _m(32.0, 240.0, 10.0, 10.0), 12: _m(28.0, 280.0, 12.0, 10.0),
               16: _m(24.0, 260.0, 14.0, 12.0), 24: _m(21.0, 255.0, 14.0, 12.0)}
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
    assert builder.REQUIRED == probe.REQUIRED


def test_real_http_wave_measures_completion_tokens_latency_and_parallelism() -> None:
    """2026-09-10 closed-loop rewrite. Real per-request latency (a deterministic
    0.08s sleep, well past the tiny 0.02s wave duration) deliberately makes
    every real completion land AFTER the nominal window closes -- this proves
    the steady-state filter really excludes overshoot completions instead of
    silently counting everything, while duration_latency_multiple=0.0 keeps
    duration_s pinned at min_duration_s so the single round stays deterministic
    (0.08s actual >> 0.02s budget, immune to scheduler jitter)."""
    sleep_s = 0.08

    class Handler(http.server.BaseHTTPRequestHandler):
        active = 0
        max_active = 0
        lock = threading.Lock()
        all_requests_arrived = threading.Barrier(4)

        def do_POST(self):  # noqa: N802
            size = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(size))
            content = body["messages"][1]["content"]
            # probe_wave()'s uncounted warm-up request (2026-09-10) arrives
            # once, synchronously, BEFORE the four measured threads start --
            # it must not touch the barrier (sized for exactly 4 parties) or
            # this test's overlap-of-4 guarantee below.
            if content == "warmup":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "choices": [{"message": {"content": "warm"}}],
                    "usage": {"completion_tokens": 1},
                }).encode())
                return
            assert content.startswith("chunk-")
            with self.lock:
                self.__class__.active += 1
                self.__class__.max_active = max(self.__class__.max_active, self.__class__.active)
            # Deterministic overlap: no response may leave until all four handlers
            # have arrived. This tests real parallel requests without scheduler timing.
            self.__class__.all_requests_arrived.wait(timeout=5)
            time.sleep(sleep_s)
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
            f"http://127.0.0.1:{server.server_address[1]}", "model", "key", rows, 4, 2048, 2,
            min_duration_s=0.02, duration_latency_multiple=0.0, ramp_fraction=0.25,
        )
        assert Handler.max_active == 4
        assert wave["requests"] == 4  # exactly one closed-loop round, no repeats
        samples = wave["samples"]
        assert all(row["out_tokens"] == 4 for row in samples)
        expected_speed = statistics.mean(
            row["out_tokens"] / row["elapsed_s"] for row in samples
        )
        assert wave["mean_single_tok_s"] == pytest.approx(expected_speed, rel=1e-9)
        assert wave["max_latency_s"] == max(row["elapsed_s"] for row in samples)
        assert wave["median_latency_s"] == statistics.median(row["elapsed_s"] for row in samples)
        expected_aggregate = sum(row["out_tokens"] for row in samples) / max(
            row["end_t"] for row in samples
        )
        assert wave["aggregate_tok_per_s"] == pytest.approx(expected_aggregate, rel=1e-9)
        # duration_latency_multiple=0.0 pins duration_s at min_duration_s exactly --
        # not derived from the real (slower) measured latency.
        assert wave["duration_s"] == pytest.approx(0.02, abs=1e-9)
        assert wave["window_start_s"] == pytest.approx(0.005, abs=1e-9)
        # every real completion lands at ~0.08s, past the 0.02s window close --
        # the steady-state filter must exclude all of them, not just some.
        assert wave["steady_state_tok_per_s"] == 0.0
        assert wave["window_completions"] == 0
        # 2026-09-10 (5th real trial): first-class fields, not reconstructed
        # after the fact from a container log.
        assert wave["max_in_flight"] == 4  # matches Handler.max_active, event-counted independently
        assert len(wave["distinct_prompt_hashes"]) == 4  # one per distinct chunk-N prompt, no repeats
    finally:
        server.shutdown()


def test_probe_wave_fires_one_uncounted_warmup_request_before_timing_starts() -> None:
    """2026-09-10, orchestrator-reported: a real cloud N=8 wave hit
    max_latency 41.4s vs median 25.1s (1.65x, close to the 2x backlog
    threshold) and a local run reproduced the same shape outright at N=2 --
    both from the FIRST tier's own cold start, no prior wave having warmed
    the server up. The warm-up request must be sent, must be discarded (not
    counted toward wave["requests"]/samples), and must use the generic
    sentinel content, not one of the measured pool's own prompts."""
    received: list[dict[str, Any]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            size = int(self.headers.get("Content-Length", "0"))
            received.append(json.loads(self.rfile.read(size)))
            time.sleep(0.05)  # dominates min_duration_s below -> exactly one real round
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
        rows = [{"system": "s", "user": "chunk-0"}]
        wave = probe.probe_wave(
            f"http://127.0.0.1:{server.server_address[1]}", "model", "key", rows, 1, 2048, 2,
            min_duration_s=0.02, duration_latency_multiple=0.0, ramp_fraction=0.25,
        )
        assert wave["requests"] == 1  # the warm-up call must not be counted
        assert len(received) == 2  # warm-up + the one real (measured) request
        assert received[0]["messages"][1]["content"] == "warmup"
        assert received[1]["messages"][1]["content"] == "chunk-0"
    finally:
        server.shutdown()


def test_wave_metrics_excludes_ramp_up_tokens_from_steady_state() -> None:
    """fable's mutation check (2026-09-10) found the steady-state window's
    ramp exclusion had ZERO test coverage: setting STEADY_STATE_RAMP_FRACTION
    to 0.0, or loosening the window's lower bound to `0 <= c["end_t"]`, both
    left the full 52-test suite green. Two ramp-up completions (end_t inside
    the excluded first 25%) carry deliberately extreme token counts a buggy
    (unguarded) accounting would surface immediately."""
    completions = [
        {"end_t": 5.0, "elapsed_s": 1.0, "out_tokens": 100_000.0, "tok_per_s": 100_000.0},
        {"end_t": 10.0, "elapsed_s": 1.0, "out_tokens": 100_000.0, "tok_per_s": 100_000.0},
        {"end_t": 40.0, "elapsed_s": 5.0, "out_tokens": 50.0, "tok_per_s": 10.0},
        {"end_t": 70.0, "elapsed_s": 5.0, "out_tokens": 50.0, "tok_per_s": 10.0},
        {"end_t": 95.0, "elapsed_s": 5.0, "out_tokens": 50.0, "tok_per_s": 10.0},
    ]
    # No ramp_fraction passed here on purpose: this must exercise the real
    # STEADY_STATE_RAMP_FRACTION constant's default, not a value hardcoded in
    # the test -- otherwise mutating the constant itself would slip past this
    # test undetected (exactly the shape of gap fable found).
    assert probe.STEADY_STATE_RAMP_FRACTION == 0.25
    excluding_ramp = probe._wave_metrics_from_completions(completions, 100.0)
    assert excluding_ramp["window_start_s"] == 25.0
    assert excluding_ramp["window_completions"] == 3  # not 0, not all 5
    # Only the 3 in-window completions (150 tokens over 75s) count -- the two
    # 100_000-token ramp-up outliers must be fully excluded, not diluted in.
    assert excluding_ramp["steady_state_tok_per_s"] == pytest.approx(150.0 / 75.0)

    # Reverse assertion: with ramp_fraction=0.0 the SAME completions must
    # produce a materially different estimate -- if this test only checked
    # the ramp_fraction=0.25 case, deleting the exclusion (ramp_fraction
    # hardcoded to 0, or the >= window_start_s check dropped) would still
    # pass it by coincidence.
    including_ramp = probe._wave_metrics_from_completions(completions, 100.0, 0.0)
    assert including_ramp["window_start_s"] == 0.0
    assert including_ramp["steady_state_tok_per_s"] == pytest.approx(200_150.0 / 100.0)
    assert including_ramp["steady_state_tok_per_s"] != excluding_ramp["steady_state_tok_per_s"]


def test_parse_candidates_override_accepts_comma_separated_ascending_ints() -> None:
    assert probe.parse_candidates_override("") is None
    assert probe.parse_candidates_override("  ") is None
    assert probe.parse_candidates_override("2,4,8,16") == (2, 4, 8, 16)
    assert probe.parse_candidates_override(" 2 , 4 ") == (2, 4)


@pytest.mark.parametrize(
    "raw",
    ["2,abc", "0,4", "-1,4", "4,2", "2,2,4"],
)
def test_parse_candidates_override_rejects_malformed_input(raw: str) -> None:
    with pytest.raises(ValueError):
        probe.parse_candidates_override(raw)


def test_candidates_within_cap_accepts_an_override_ladder_not_just_candidates() -> None:
    """2026-09-10: a prior round temporarily edited the tracked CANDIDATES
    constant for a local test run and left it in the working tree, which
    corrupted a second collaborator's independent test results on that same
    commit. The fix is this parameter: local/mechanism testing overrides the
    ladder via --candidates/PROBE_CANDIDATES, never by editing source."""
    override = (2, 4, 8, 16)
    assert probe.candidates_within_cap(None, override) == override
    assert probe.candidates_within_cap(10, override) == (2, 4, 8)
    assert probe.candidates_within_cap(1, override) == (2,)  # smallest still tried alone


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


def test_retract_signal_prefers_exact_sglang_metric_and_records_line() -> None:
    exact_line = 'sglang:num_retracted_reqs{engine_type="unified",pid="243"} 0.0'
    snapshot = {
        "server_info": {"alive": True, "body": '{"retract_check_timestamp":1781619500.97}'},
        "metrics": {"alive": True, "body": "unrelated_retract_total 99\n" + exact_line},
    }
    details = probe.retract_signal_details(snapshot)
    assert details["value"] == 0.0
    assert details["match"] == "exact"
    assert [entry["line"] for entry in details["lines"]] == [exact_line]


def test_retract_signal_supports_exact_vllm_preemption_metric() -> None:
    line = 'vllm:num_preemptions_total{model_name="m"} 2.0'
    details = probe.retract_signal_details({"metrics": {"body": line}})
    assert details["value"] == 2.0
    assert details["match"] == "exact"
    assert details["lines"][0]["line"] == line


@pytest.mark.parametrize("generic_metric", ["custom_retract_total", "custom_preempt_total"])
def test_retract_signal_generic_fallback_excludes_comments_and_time_metrics(
    generic_metric: str,
) -> None:
    snapshot = {
        "server_info": {"body": '{"num_retracted_reqs": 1781619500.97}'},
        "metrics": {
            "body": (
                "# HELP custom_retract_total timestamp 1781619500.97\n"
                "# TYPE custom_retract_total gauge\n"
                "custom_retract_created 1781619500.97\n"
                "custom_retract_timestamp_seconds 1781619500.97\n"
                "custom_retract_runtime_seconds 1781619500.97\n"
                f'{generic_metric}{{gpu="0"}} 3.0'
            )
        },
    }
    details = probe.retract_signal_details(snapshot)
    assert details["value"] == 3.0
    assert details["match"] == "generic_prometheus"
    assert [entry["metric"] for entry in details["lines"]] == [generic_metric]


def _real_sglang_metrics_fixture() -> str:
    path = REPO / "test" / "fixtures" / "adaptive-concurrency-evidence-001-metrics.txt"
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "2712ae207b3e62b9e0b814e25415bab9024a2a8939c318a55ac499e36bc28912"
    return raw.decode("utf-8")


def test_real_metrics_fixture_reads_zero_not_timestamp() -> None:
    metrics = _real_sglang_metrics_fixture()
    details = probe.retract_signal_details(
        {"server_info": {"body": ""}, "metrics": {"body": metrics}}
    )
    assert details["value"] == 0.0
    assert details["match"] == "exact"
    assert len(details["lines"]) == 1
    assert details["lines"][0]["metric"] == "sglang:num_retracted_reqs"
    assert details["lines"][0]["line"].endswith(" 0.0")


def test_real_metrics_fixture_allows_observed_n8_wave_to_pass() -> None:
    metrics_body = _real_sglang_metrics_fixture()
    increased = metrics_body.replace(
        'sglang:num_retracted_reqs{engine_type="unified",model_name="Jackrong/Qwopus3.6-27B-v2-FP8",moe_ep_rank="0",pid="243",pp_rank="0",tp_rank="0"} 0.0',
        'sglang:num_retracted_reqs{engine_type="unified",model_name="Jackrong/Qwopus3.6-27B-v2-FP8",moe_ep_rank="0",pid="243",pp_rank="0",tp_rank="0"} 1.0',
    )
    evidence = _evidence_from_bodies([
        ("", metrics_body, True),  # before N=8
        ("", metrics_body, True),  # after N=8: true counter unchanged
        ("", metrics_body, True),  # before N=12
        ("", increased, True),     # stop after proving N=8 passed
    ])
    wave_metrics = {
        8: _m(25.0, 200.0, 40.5, 25.4),  # observed lag ratio 1.59 < fallback 2x
        12: _m(23.0, 230.0, 30.0, 24.0),
    }
    selected, waves, passed, _timeout = probe.choose_concurrency(
        _requests(), _fake_wave(wave_metrics, []), fetch_evidence=evidence
    )
    assert selected == 8 and passed is True
    assert waves[0]["passed"] is True
    assert waves[0]["backlog_gate_used"] == "retract"
    assert waves[0]["retract_before"] == waves[0]["retract_after"] == 0.0
    assert waves[0]["retract_evidence_before"]["lines"][0]["line"].endswith(" 0.0")


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
        ("{}", "sglang:num_retracted_reqs 0.0", True),  # before N=8
        ("{}", "sglang:num_retracted_reqs 0.0", True),  # after N=8
        ("{}", "sglang:num_retracted_reqs 0.0", True),  # before N=12
        ("{}", "sglang:num_retracted_reqs 3.0", True),  # after N=12 -- real counter rose
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


def test_backlog_gate_falls_back_when_before_after_counter_series_differ() -> None:
    seen: list[tuple[int, list[str]]] = []
    metrics = {8: _m(32.0, 240.0, 25.0, 10.0)}  # latency fallback must fail at 2x
    evidence = _evidence_from_bodies([
        ("", 'custom_preempt_total{worker="old"} 100', True),
        ("", 'sglang:num_retracted_reqs{worker="new"} 1', True),
    ])
    selected, waves, passed, _timeout = probe.choose_concurrency(
        _requests(), _fake_wave(metrics, seen), fetch_evidence=evidence
    )
    assert selected == 8 and passed is False
    assert waves[0]["retract_before"] == 100.0
    assert waves[0]["retract_after"] == 1.0
    assert waves[0]["retract_series_compatible"] is False
    assert waves[0]["backlog_gate_used"] == "latency_fallback"
    assert waves[0]["backlog_ok"] is False


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
                {"system": "s", "user": "u"}, 2048, 5,
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


def test_looks_like_server_info_requires_model_path() -> None:
    assert probe._looks_like_server_info('{"model_path": "X", "context_length": 16384}') is True


def test_looks_like_server_info_rejects_real_401_capture() -> None:
    """Regression for the exact false positive on the 4th real SGLang fp8
    trial: a missing --api-key made every request 401, and the body
    {"error": "Unauthorized"} is valid JSON and an object -- a shape-blind
    check ("did this parse, is it a dict") would wrongly call this alive."""
    assert probe._looks_like_server_info('{"error": "Unauthorized"}') is False


def test_looks_like_server_info_rejects_non_json_body() -> None:
    assert probe._looks_like_server_info("<html>404 Not Found</html>") is False


def test_looks_like_metrics_requires_a_real_prometheus_sample() -> None:
    assert probe._looks_like_metrics('sglang:num_retracted_reqs{engine="0"} 0.0') is True


def test_looks_like_metrics_rejects_json_error_body() -> None:
    assert probe._looks_like_metrics('{"error": "Unauthorized"}') is False


def test_fetch_evidence_snapshot_downgrades_alive_when_body_shape_is_wrong() -> None:
    """"Got a response" only proves liveness when the response's shape is
    right -- 401/403/404, or an HTML error page, can all avoid a connection-
    level exception while carrying none of the shape either endpoint should
    have. Real HTTP server standing in for the actual 401 capture."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b'{"error": "Unauthorized"}'
            self.send_response(401)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        snapshot = probe.fetch_evidence_snapshot(f"http://127.0.0.1:{server.server_address[1]}", "")
        # A connection-level exception did NOT happen (status_code is present),
        # yet the shape check must still call this not-alive.
        assert snapshot["server_info"]["status_code"] == 401
        assert snapshot["server_info"]["alive"] is False
        assert snapshot["metrics"]["alive"] is False
    finally:
        server.shutdown()


def test_choose_concurrency_respects_a_filtered_candidates_subset() -> None:
    """spec-07, 3rd real trial: a candidate above a hard server-side ceiling
    measures queuing behind that ceiling, not real concurrency. Only
    metrics for {8, 12} exist -- if the climb ever tried 16, this would
    KeyError inside the fake wave, proving the filter actually bounds the
    ladder rather than just relabeling it after the fact."""
    seen: list[tuple[int, list[str]]] = []
    metrics = {8: _m(32.0, 240.0, 10.0, 10.0), 12: _m(28.0, 280.0, 10.0, 10.0)}
    selected, waves, passed, _timeout = probe.choose_concurrency(
        _requests(), _fake_wave(metrics, seen), candidates=(8, 12)
    )
    assert selected == 12
    assert passed is True
    assert len(waves) == 2


def test_choose_concurrency_rejects_empty_candidates() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        probe.choose_concurrency(_requests(), lambda _batch, _n: {}, candidates=())


def test_candidates_within_cap_returns_full_ladder_when_no_cap_reported() -> None:
    """No cap found in the container log means "not capped", not "unknown" --
    the full CANDIDATES ladder applies unchanged."""
    assert probe.candidates_within_cap(None) == probe.CANDIDATES


def test_candidates_within_cap_filters_to_reported_ceiling() -> None:
    assert probe.candidates_within_cap(20) == (8, 12, 16)


def test_candidates_within_cap_is_inclusive_of_an_exact_candidate_value() -> None:
    """A cap that lands exactly ON a candidate keeps that candidate -- the
    server said it can handle up to and including this many."""
    assert probe.candidates_within_cap(16) == (8, 12, 16)


def test_candidates_within_cap_keeps_smallest_when_cap_is_below_it() -> None:
    """A cap below even the smallest candidate (e.g. cap=5) still tries N=8
    alone rather than refusing to probe at all -- one real measurement beats
    none, and the output records candidates[0] > effective_cap for whoever
    reads the result."""
    assert probe.candidates_within_cap(5) == (8,)


def test_probe_own_default_max_tokens_matches_translator_single_source() -> None:
    """The probe used to hardcode its own DEFAULT_MAX_TOKENS = 2048, a
    separate copy of the same number the translator resolves via
    providers.omlx_provider.DEFAULT_MAX_TOKENS — the exact duplication
    pattern spec-05 exists to remove."""
    import sys
    sys.path.insert(0, str(REPO / "scripts"))
    from providers.omlx_provider import DEFAULT_MAX_TOKENS as translator_default
    assert probe.DEFAULT_MAX_TOKENS is translator_default


# ---------- spec-09 calibration tests (verifier/fable spec, 2026-09-10) ----------
# These feed choose_tier() fixed numbers from a real three-tier sustained-load
# trial (bench/sweep-sglang-fp8-layer3.json + snap-08.../N24, N32) plus the
# transitional single-wave probe data from the run that motivated this whole
# redesign. No HTTP, no clock -- see test/fixtures/probe_calibration/.

CALIBRATION_DIR = REPO / "test" / "fixtures" / "probe_calibration"


def _load_calibration_fixture(name: str) -> dict[str, Any]:
    return json.loads((CALIBRATION_DIR / name).read_text(encoding="utf-8"))


def test_probe_selects_same_tier_as_sustained_sweep_on_fixture() -> None:
    """Main assertion (spec-09 S2): fed the real sustained-load throughput for
    N=16/24/32 (270.5/342.4/414.9 tok/s, all safety_ok), the selector must
    pick 32 -- the true best tier on real hardware.

    Mutation check (run once, recorded here): reverting the asymmetric rule to
    the OLD "stop on first tier that fails to gain >=10% over the previous
    tier" rule, fed these SAME correct sustained values, still returns 32 --
    proving the primary assertion alone cannot tell old code from new code.
    Only feeding the OLD rule the OLD wrong single-wave values (256.9, 265.7,
    -- see test_probe_steady_estimate_..._TRANSITIONAL below) turns it red
    (selects 16). This is exactly why S3's magnitude assertions exist
    alongside this one.
    """
    levels = [
        probe.LevelResult(n=16, steady_tok_per_s=270.5, safety_ok=True),
        probe.LevelResult(n=24, steady_tok_per_s=342.4, safety_ok=True),
        probe.LevelResult(n=32, steady_tok_per_s=414.9, safety_ok=True),
    ]
    assert probe.choose_tier(levels) == 32


def test_probe_rejects_queuing_plateau_via_marginal_efficiency_5th_trial_fixture() -> None:
    """5th real trial (review-probe-v2-cloud-acceptance.md): candidates
    8/12/16/24/32/48 on a server whose own --max-running-requests=32, 48
    deliberately above that to force a real queuing plateau. Real steady-
    state: 178.5/276.6/310.5/418.9/470.7/487.1. 48 beats 32 by +3.5% purely
    by queuing (container log: running-req peaked at 32 throughout the 48
    tier, queue-req peaked at 38) -- the OLD "any positive gain is a new
    best" rule selected 48 for exactly this reason.

    Mutation check (run once, recorded here): reverting to plain
    `steady_tok_per_s > best_tp` (no marginal-efficiency gate) fed these SAME
    real numbers selects 48. Confirmed red against the real answer (32).
    """
    levels = [
        probe.LevelResult(n=8, steady_tok_per_s=178.5, safety_ok=True),
        probe.LevelResult(n=12, steady_tok_per_s=276.6, safety_ok=True),
        probe.LevelResult(n=16, steady_tok_per_s=310.5, safety_ok=True),
        probe.LevelResult(n=24, steady_tok_per_s=418.9, safety_ok=True),
        probe.LevelResult(n=32, steady_tok_per_s=470.7, safety_ok=True),
        probe.LevelResult(n=48, steady_tok_per_s=487.1, safety_ok=True),
    ]
    assert probe.choose_tier(levels) == 32


def test_clears_marginal_efficiency_bar_matches_real_517_8_threshold() -> None:
    """Locks in the exact formula/threshold value the orchestrator quoted
    from the 5th trial: 470.7 * (1 + 0.2 * (48/32 - 1)) = 517.8."""
    required = 470.7 * (1.0 + probe.MARGINAL_EFFICIENCY_THRESHOLD * (48 / 32 - 1.0))
    assert required == pytest.approx(517.8, abs=0.05)
    assert not probe._clears_marginal_efficiency_bar(48, 487.1, 32, 470.7)  # 487.1 < 517.8
    assert probe._clears_marginal_efficiency_bar(48, 520.0, 32, 470.7)  # a real win clears it
    # The very first tier always becomes best unconditionally (best_tp is
    # still -inf; there is nothing yet to compute a ratio against).
    assert probe._clears_marginal_efficiency_bar(8, 1.0, 8, float("-inf"))


def test_kv_cache_candidate_cap_matches_5th_trial_arithmetic() -> None:
    """129742 // 3700 = 35 (5th trial's own ready-summary max_total_num_tokens)
    -- an arithmetic fact, not a statistical judgment; N=48 exceeds it and
    N=32 doesn't, so this alone would have excluded 48 before spending a
    single wave on it."""
    assert probe.kv_cache_candidate_cap(129_742) == 35
    assert probe.candidates_within_cap(35, (8, 12, 16, 24, 32, 48)) == (8, 12, 16, 24, 32)
    with pytest.raises(ValueError):
        probe.kv_cache_candidate_cap(0)
    with pytest.raises(ValueError):
        probe.kv_cache_candidate_cap(129_742, tokens_per_request=0)


def test_probe_selects_best_not_highest_when_higher_tier_is_worse() -> None:
    """Locks in "asymmetric + select best" (spec-09 S2, second case)."""
    # 32 is the LAST tier tried and is 11.8% below the best (24) -- past the
    # 5% tolerance -- so the best tier (24), not the last one, is returned.
    levels_a = [
        probe.LevelResult(n=16, steady_tok_per_s=300.0, safety_ok=True),
        probe.LevelResult(n=24, steady_tok_per_s=340.0, safety_ok=True),
        probe.LevelResult(n=32, steady_tok_per_s=300.0, safety_ok=True),
    ]
    assert probe.choose_tier(levels_a) == 24
    # 2026-09-10 (5th real trial, review-probe-v2-cloud-acceptance.md): 32's
    # 4.4% dip below 24's 340 does NOT cross the 5% stopping tolerance, so
    # the climb is not even considered a miss -- but 48's +1.5% over 24
    # (345 vs 340) also fails to clear the marginal-efficiency bar for a new
    # best (needs 340*(1+0.2*(48/24-1))=408), so the queuing-plateau shape
    # this fixture models must select 24, not 48.
    levels_b = [
        probe.LevelResult(n=16, steady_tok_per_s=300.0, safety_ok=True),
        probe.LevelResult(n=24, steady_tok_per_s=340.0, safety_ok=True),
        probe.LevelResult(n=32, steady_tok_per_s=325.0, safety_ok=True),
        probe.LevelResult(n=48, steady_tok_per_s=345.0, safety_ok=True),
    ]
    assert probe.choose_tier(levels_b) == 24


def test_probe_steady_estimate_within_20pct_of_sweep__TRANSITIONAL_tolerance_until_closed_loop_fixture_exists() -> None:
    """Secondary/magnitude assertion (spec-09 S3). No closed-loop fixture
    exists yet (needs a first real-machine run of the new probe_wave()), so
    the transitional estimate is N x mean_single_tok_s from the last
    single-wave probe run that motivated this redesign.

    Tolerance provenance (not a design value): 20% was calibrated against
    THIS trial's own transitional estimates (+10%/+17% vs sweep) and the old
    method's known-bad value (-22.4%) -- wide enough to pass a reasonable
    transitional estimate, narrow enough to redden the old method. Once the
    closed-loop probe has real fixture data, replace this estimate with its
    own steady_state_tok_per_s and tighten to 10% (see the TRANSITIONAL
    marker in this test's own name)."""
    sweep = {
        16: _load_calibration_fixture("sustained-sweep-N16.json")["tok_per_s"],
        24: _load_calibration_fixture("sustained-sweep-N24.json")["tok_per_s"],
    }
    trial4 = _load_calibration_fixture("trial4-single-wave-probe.json")
    mean_single = {w["n"]: w["mean_single_tok_s"] for w in trial4["waves"]}
    probe_est = {n: n * mean_single[n] for n in sweep}

    for n in sweep:
        rel_err = abs(probe_est[n] - sweep[n]) / sweep[n]
        assert rel_err <= 0.20, f"N={n}: probe_est={probe_est[n]:.1f} sweep={sweep[n]:.1f}"

    # Must-be-red case: the OLD method's own value at N=24 (aggregate_tok_per_s
    # from this same single-wave run) fails this exact tolerance.
    old_method_n24 = next(w for w in trial4["waves"] if w["n"] == 24)["aggregate_tok_per_s"]
    old_rel_err = abs(old_method_n24 - sweep[24]) / sweep[24]
    assert old_rel_err > 0.20
    assert old_rel_err == pytest.approx(0.224, abs=0.005)


def test_probe_gain_between_tiers_within_10_points_of_sweep_gain__TRANSITIONAL() -> None:
    """Secondary/magnitude assertion (spec-09 S3), the gain-delta half: the
    16->24 gain the probe estimates must land within 10 points of the true
    sustained-load gain (+26.6%). Same TRANSITIONAL caveat as the test
    above -- tighten to 5 points once closed-loop fixture data exists."""
    sweep_16 = _load_calibration_fixture("sustained-sweep-N16.json")["tok_per_s"]
    sweep_24 = _load_calibration_fixture("sustained-sweep-N24.json")["tok_per_s"]
    gain_sweep = sweep_24 / sweep_16 - 1.0

    trial4 = _load_calibration_fixture("trial4-single-wave-probe.json")
    mean_single = {w["n"]: w["mean_single_tok_s"] for w in trial4["waves"]}
    probe_est_16 = 16 * mean_single[16]
    probe_est_24 = 24 * mean_single[24]
    gain_probe = probe_est_24 / probe_est_16 - 1.0
    assert abs(gain_probe - gain_sweep) <= 0.10

    # Must-be-red case: the old method's own 16/24 aggregate values measured a
    # gain 23.2 points off the true value -- far past this same tolerance.
    old_16 = next(w for w in trial4["waves"] if w["n"] == 16)["aggregate_tok_per_s"]
    old_24 = next(w for w in trial4["waves"] if w["n"] == 24)["aggregate_tok_per_s"]
    gain_old = old_24 / old_16 - 1.0
    assert abs(gain_old - gain_sweep) > 0.10


def test_safety_gates_unchanged_by_saturation_redesign() -> None:
    """spec-09 S4: the redesign must not have touched the error/retract/
    backlog-latency safety gates -- confirm their existing regression tests
    are still present in this module (not renamed away or deleted) rather
    than re-deriving their logic here."""
    required_test_names = [
        "test_retract_signal_prefers_exact_sglang_metric_and_records_line",
        "test_retract_signal_supports_exact_vllm_preemption_metric",
        "test_backlog_gate_prefers_retract_signal_over_latency",
        "test_backlog_gate_falls_back_when_before_after_counter_series_differ",
        "test_backlog_latency_fallback_tightened_to_2x_not_3x",
        "test_wave_failure_with_no_liveness_response_raises_server_died_error",
        "test_wave_failure_with_server_alive_retreats_to_last_good_tier_normally",
    ]
    for name in required_test_names:
        assert callable(globals().get(name)), f"missing safety-gate regression test: {name}"

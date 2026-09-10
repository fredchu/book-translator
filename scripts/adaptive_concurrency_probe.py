#!/usr/bin/env python3
"""Measure a server's safe client concurrency with disjoint, production-sized prompts.

spec-05 (2026-09-10): the previous version gated selection on an ABSOLUTE
threshold derived from a hardcoded timeout (max_tokens / (timeout * 0.8)).
That timeout (120s) was never the real production value — the translator
always passes its own --timeout (1800 by default), so the gate was checking
against a number the real system didn't use. The math itself also proves the
gate unnecessary: once a request's timeout comfortably exceeds
max_tokens / single-request-speed, a runaway generation ends cleanly at
max_tokens regardless of how much larger the timeout is — the timeout no
longer bounds anything. So selection now uses two RELATIVE gates that need no
externally-supplied timeout at all:
  - backlog: this wave's worst latency must stay within LATENCY_MULTIPLE of
    this wave's own median (catches a request stuck behind cache
    eviction/preemption, independent of absolute scale)
  - saturation: originally "this wave's AGGREGATE (whole-wave) throughput
    must beat the previous (smaller) tier by at least a gain threshold" —
    SUPERSEDED by the second redesign below (see the module-level comment
    above SATURATION_DEGRADATION_TOLERANCE and choose_tier()'s docstring);
    kept here as history, not as current behavior.
`max_tokens` no longer gates anything during selection; it only feeds the
timeout DERIVED from whichever tier gets selected (see derive_timeout()),
which cloud_llm.sh then hands to both this probe's own future runs and the
real translator — one measurement, one number, fed to both call sites.

spec-07 (2026-09-10, SGLang fp8 real-machine trial): two corrections on top
of the above, both found in review before any money was spent on a live run.

1. "Gate failed" and "the server process died" are different events and must
   never be reported the same way. A wave's HTTP call can fail (500,
   connection refused, timeout) for two very different reasons: the server
   is still up but this concurrency level overloaded it (a normal, USEFUL
   early stop — exactly what this probe exists to find), or the whole
   process crashed (nothing further should run against it, least of all a
   real book). So on any wave failure this module now calls
   fetch_evidence() — a GET to /get_server_info and /metrics — as a liveness
   check: a response (any status code) means the frontend is alive and this
   was a normal gate failure, retreat to the last confirmed-safe tier and
   return normally (`passed=True`, no exception); no response at all
   (connection-level failure) means the process is gone, raise
   ServerDiedError so the caller refuses to translate against a dead server.
   The earlier version treated every failure identically as "probe broke,
   fall back to a hardcoded default and translate anyway" — which is
   precisely how the first real SGLang trial ran 120 prompts against an
   already-dead server.

2. The backlog gate's LATENCY_MULTIPLE (3.0) was too loose to catch a real
   observed straggler (2.44x its own median) on real hardware. Rather than
   just lowering that shared constant (which would also shrink
   derive_timeout()'s unrelated safety margin — a different concern that
   happens to reuse the same wave measurements), the backlog gate now
   prefers a DIRECT signal when available: whether the server's own retract
   counter increased during the wave (fetched via the same fetch_evidence()
   snapshot, before and after). Falls back to a latency multiple — now a
   SEPARATE, tighter BACKLOG_FALLBACK_LATENCY_MULTIPLE (2.0) — only when no
   retract/preempt counter can be found in the diagnostic metrics. Counter
   discovery now prefers the observed exact SGLang/vLLM names, then falls
   back to strict Prometheus samples while excluding timestamp-like metric
   names. The exact lines used are persisted in each wave's JSON.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, NamedTuple

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from providers.omlx_provider import DEFAULT_MAX_TOKENS  # noqa: E402  single source with the translator

CANDIDATES = (8, 12, 16, 24, 32)
# Single source for build_adaptive_probe_load.py's "full" mode pool size
# (pi's redesign, 2026-09-10, imports this directly rather than keeping its
# own copy of the formula — the exact duplication shape spec-05 exists to
# remove). 230 = 2.5 x sum(CANDIDATES) (92): choose_concurrency() below
# splits the pool PROPORTIONALLY by each candidate's own N, so every tier
# ends up with roughly 2.5x its own N distinct prompts to cycle through
# during its closed-loop window -- the old design only needed exactly N per
# tier (one shot, fire-and-wait), but the closed loop can complete many more
# than N requests per tier and heavy repetition would bias the measurement.
REQUIRED = 230
# Ceiling for the PROBE'S OWN measurement HTTP calls only — unrelated to the
# derived production timeout this script outputs. Generous on purpose: an
# artificially tight measurement timeout would truncate a real slow wave and
# corrupt the derived value, which is exactly the failure mode this script
# exists to avoid.
DEFAULT_PROBE_TIMEOUT = 120.0
TIMEOUT_HEADROOM = 0.8
# Used ONLY by derive_timeout()'s production-timeout safety margin — NOT the
# backlog gate below. Keeping these as two separate constants (spec-07
# review) means tightening the gate never silently shrinks the timeout
# margin, and vice versa; they measure different things that happen to both
# read wave["max_latency_s"].
LATENCY_MULTIPLE = 3.0
# Used ONLY as the backlog gate's FALLBACK when no retract signal is
# available (see choose_concurrency()). Tighter than LATENCY_MULTIPLE above
# on purpose: a real observed straggler on SGLang fp8 hit 2.44x its wave's
# own median, which the old shared 3.0x constant would have let through.
BACKLOG_FALLBACK_LATENCY_MULTIPLE = 2.0

# ---------- 2026-09-10 second redesign: closed-loop measurement + asymmetric gate ----------
# Real hardware (4th SGLang fp8 trial) proved the FIRST redesign's saturation gate still
# under-measured higher N: the old probe_wave() fired N requests once simultaneously and
# measured wall-clock of the whole batch, which is bound by the slowest member AND includes
# every request's cold-start ramp. That made 16->24 look like +3.4% (SATURATION_GAIN's old
# 10% threshold stopped the climb at 16), while a real 10xN sustained-load sweep measured
# +26.6%, and 32 a further +21.2% on top of that -- the old single-shot method was measuring
# the wrong thing, not just gating it too strictly.
#
# Fix has two independent parts:
# 1. probe_wave() now runs a CLOSED-LOOP fixed-duration measurement: N slots stay
#    continuously busy for T = max(MIN_WAVE_DURATION_S, WAVE_DURATION_LATENCY_MULTIPLE x this
#    wave's own median single-request latency) seconds, each slot immediately dispatching a
#    replacement request as soon as its previous one completes. Only tokens completed within
#    the steady-state window [T x STEADY_STATE_RAMP_FRACTION, T] count toward
#    steady_state_tok_per_s -- the first quarter is the ramp-up (no steady-state analogue
#    while all N slots are still starting cold) and anything finishing after T is a straggler
#    outside the window. The OLD metric (aggregate_tok_per_s: total tokens / whole-run wall
#    clock) is still computed and recorded for reference, but no longer drives selection.
# 2. choose_tier() replaces the old "stop at the first tier that doesn't beat the previous by
#    SATURATION_GAIN" rule with an asymmetric one: since one extra tier costs about a minute
#    but a false early stop can hide 53% of real throughput, climbing NEVER stops just because
#    a tier didn't set a new best -- only when a tier's steady-state throughput falls more
#    than SATURATION_DEGRADATION_TOLERANCE below the best tier seen so far, AND the very next
#    tier ALSO fails to recover to within that tolerance, does it stop. Selection is always
#    whichever tier had the best throughput measured overall, not the highest N or the last
#    one tried -- a real "higher N is actually worse" case must still be caught.
# Both parts are independently necessary: with the real (correct) numbers, even the OLD
# threshold-style gate would have climbed to 32 (26.6%/21.2% both clear the old 10% bar) --
# see choose_tier()'s docstring for the mutation check proving the SELECTION rule alone was
# never the primary bug, the MEASUREMENT was.
MIN_WAVE_DURATION_S = 60.0
WAVE_DURATION_LATENCY_MULTIPLE = 3.0
STEADY_STATE_RAMP_FRACTION = 0.25
SATURATION_DEGRADATION_TOLERANCE = 0.05
DIAGNOSTIC_TIMEOUT_S = 10.0
# ---------- warm-up request (2026-09-10, orchestrator-reported) ----------
# Every tier's closed loop is the FIRST traffic that tier's own slots ever
# send -- there is no prior wave's requests to have already absorbed a
# one-time cold-start cost (model paging in, thread/connection setup). A
# real 4th SGLang trial's N=8 wave hit max_latency 41.4s vs median 25.1s
# (1.65x, uncomfortably close to the 2x backlog threshold) -- and a local
# oMLX run reproduced the SAME shape outright at N=2 (23.5s vs 9.7s median,
# tripping the gate) purely from this effect, with the machine otherwise
# idle. One request, fired and DISCARDED before the timed window starts,
# absorbs that cost so it never lands inside max_latency/median/duration and
# never counts toward completions or tokens. Deliberately generic/unrelated
# to the measured pool (not one of `requests_`) so it can't prefix-cache-warm
# a prompt that WILL be measured -- that would bias the first real completion
# faster, the unsafe direction. Small max_tokens on purpose: this only needs
# to trigger the same warm-up machinery, not a full-length generation.
_WARMUP_REQUEST = {"system": "warmup", "user": "warmup"}
WARMUP_MAX_TOKENS = 8


class ServerDiedError(RuntimeError):
    """A wave's HTTP call failed AND a liveness check afterward (GET to both
    /get_server_info and /metrics) also failed to get any response at all.
    Distinct from a normal gate failure (see choose_concurrency() docstring)
    — callers must NOT fall back to a default concurrency and keep
    translating when this is raised; the server process itself is gone."""

    def __init__(self, message: str, *, waves: list[dict[str, Any]]):
        super().__init__(message)
        self.waves = waves  # evidence gathered so far, for the caller to persist even though this raised


def probe_one(
    endpoint: str,
    model: str,
    api_key: str,
    request: dict[str, Any],
    max_tokens: int,
    timeout: float,
) -> dict[str, float]:
    """One request, fire-and-measure. No synchronized-start barrier any more
    (2026-09-10 redesign) -- probe_wave() is now a closed loop where each
    slot dispatches its own replacement the instant it frees up, so there is
    no single "wave start" instant to synchronize on."""
    started = time.monotonic()
    response = requests.post(
        f"{endpoint.rstrip('/')}/v1/chat/completions",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": request["system"]},
                {"role": "user", "content": request["user"]},
            ],
            "temperature": 0.3,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        },
        headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        timeout=timeout,
    )
    elapsed = time.monotonic() - started
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        # spec-07: raise_for_status() alone discards the response body, and
        # SGLang puts the actual failure reason there — losing it is exactly
        # what made the first real trial's 500s undiagnosable after the fact.
        raise requests.HTTPError(
            f"{exc} | body[:500]={response.text[:500]!r}", response=response
        ) from exc
    payload = response.json()
    content = payload["choices"][0]["message"]["content"]
    tokens = (payload.get("usage") or {}).get("completion_tokens")
    if not isinstance(content, str) or not content or not isinstance(tokens, int) or tokens <= 0:
        raise ValueError("completion must contain non-empty content and positive completion_tokens")
    return {"elapsed_s": elapsed, "out_tokens": float(tokens), "tok_per_s": tokens / elapsed}


def probe_wave(
    endpoint: str,
    model: str,
    api_key: str,
    requests_: list[dict[str, Any]],
    n: int,
    max_tokens: int,
    timeout: float,
    *,
    min_duration_s: float = MIN_WAVE_DURATION_S,
    duration_latency_multiple: float = WAVE_DURATION_LATENCY_MULTIPLE,
    ramp_fraction: float = STEADY_STATE_RAMP_FRACTION,
) -> dict[str, Any]:
    """Closed-loop, fixed-duration, steady-state-window measurement at
    concurrency n (2026-09-10 redesign — see the module-level comment above
    SATURATION_DEGRADATION_TOLERANCE for why the old single-shot design
    systematically under-measured higher N).

    Keeps exactly n requests in flight continuously for T seconds, where
    T = max(min_duration_s, duration_latency_multiple x the median
    single-request latency observed SO FAR in this wave) -- T is not known
    up front (there is no latency estimate before the first completions
    arrive) and is recomputed after every completion, converging once enough
    samples exist. As soon as one request completes, its slot immediately
    dispatches a replacement (cycling through `requests_`; a wave that runs
    long at high n can complete far more requests than len(requests_), so
    the caller should provide enough DISTINCT prompts per tier to keep
    repeats rare — that sizing is out of scope here, see
    build_adaptive_probe_load.py).

    Only tokens from completions finishing within the steady-state window
    [T x ramp_fraction, T] count toward `steady_state_tok_per_s` — the first
    fraction is the ramp-up (all n slots starting cold has no steady-state
    analogue) and anything finishing after T is a straggler outside the
    window, same reasoning as excluding the ramp.

    A request-level exception propagates out of this function exactly like
    the old ThreadPoolExecutor.map()-based design did (first exception wins,
    every other in-flight slot's result is discarded) — the safety-gate
    behavior in choose_concurrency() that reacts to this is unchanged by
    this redesign.
    """
    if not requests_:
        raise ValueError(f"N={n} needs at least one prompt to run a closed-loop wave")

    # See the module-level comment above WARMUP_MAX_TOKENS: fired and
    # discarded BEFORE `start` below, so its cost never enters the timed
    # window. A failure here (e.g. connection refused) propagates exactly
    # like a real measured request's failure would -- choose_concurrency()'s
    # existing liveness check already handles that correctly.
    probe_one(endpoint, model, api_key, _WARMUP_REQUEST, WARMUP_MAX_TOKENS, timeout)

    lock = threading.Lock()
    completions: list[dict[str, Any]] = []
    failure: list[BaseException] = []
    duration_s = min_duration_s
    start = time.monotonic()
    counter = itertools.count()

    def worker() -> None:
        nonlocal duration_s
        while True:
            with lock:
                if failure:
                    return
                if time.monotonic() - start >= duration_s:
                    return
                req = requests_[next(counter) % len(requests_)]
            try:
                row = probe_one(endpoint, model, api_key, req, max_tokens, timeout)
            except Exception as exc:  # noqa: BLE001 -- must propagate, matches the old pool.map() behavior
                with lock:
                    if not failure:
                        failure.append(exc)
                return
            row["end_t"] = time.monotonic() - start
            with lock:
                completions.append(row)
                med = statistics.median(c["elapsed_s"] for c in completions)
                duration_s = max(min_duration_s, duration_latency_multiple * med)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if failure:
        raise failure[0]

    final_duration_s = duration_s
    metrics = _wave_metrics_from_completions(completions, final_duration_s, ramp_fraction)
    return {
        "n": n,
        "requests": len(completions),
        **metrics,
        # Keep raw measurements in telemetry so aggregation can be audited without
        # asserting anything about the runner's absolute wall-clock performance.
        "samples": completions,
    }


def _wave_metrics_from_completions(
    completions: list[dict[str, Any]],
    final_duration_s: float,
    ramp_fraction: float = STEADY_STATE_RAMP_FRACTION,
) -> dict[str, Any]:
    """Pure post-processing of a closed-loop wave's raw completions (extracted
    2026-09-10, after fable's mutation check found the steady-state window's
    ramp-exclusion had ZERO test coverage: setting STEADY_STATE_RAMP_FRACTION
    to 0.0, or loosening the window's lower bound to `0 <= c["end_t"]`, both
    left the full 52-test suite green). Factored out so the exclusion itself
    is calibration-testable with synthetic completions -- no HTTP, no
    threads, no clock; see
    test_wave_metrics_excludes_ramp_up_tokens_from_steady_state."""
    window_start_s = final_duration_s * ramp_fraction
    window_length_s = final_duration_s - window_start_s
    window_tokens = sum(
        c["out_tokens"] for c in completions
        if window_start_s <= c["end_t"] <= final_duration_s
    )
    latencies = [c["elapsed_s"] for c in completions]
    total_out_tokens = sum(c["out_tokens"] for c in completions)
    wall_clock_s = max((c["end_t"] for c in completions), default=0.0)
    return {
        "duration_s": round(final_duration_s, 3),
        "window_start_s": round(window_start_s, 3),
        # Per-REQUEST speed. Naturally DECREASES as concurrency rises (more
        # requests sharing the same GPU/scheduler) even when the server is
        # doing strictly more total work — never use this to judge whether
        # raising N is still paying off. Still needed by derive_timeout()'s
        # per-request timeout bound, which is a different question.
        "mean_single_tok_s": statistics.mean(row["tok_per_s"] for row in completions),
        # PRIMARY metric (2026-09-10 redesign): tokens completed strictly
        # within the steady-state window, divided by the window's own
        # length. This is what choose_tier() actually compares across tiers.
        "steady_state_tok_per_s": (window_tokens / window_length_s) if window_length_s > 0 else 0.0,
        # OLD metric, kept for reference only (see module comment) — total
        # tokens over the whole run's wall clock, including ramp-up and any
        # stragglers past T. No longer drives selection.
        "aggregate_tok_per_s": (total_out_tokens / wall_clock_s) if wall_clock_s else 0.0,
        "max_latency_s": max(latencies),
        "median_latency_s": statistics.median(latencies),
    }


def fetch_endpoint_text(url: str, api_key: str, timeout: float = DIAGNOSTIC_TIMEOUT_S) -> dict[str, Any]:
    """GET a diagnostic endpoint and keep its raw body verbatim — used both as
    the post-failure liveness check and as before/after evidence for the
    backlog gate. `alive` here only means "a connection-level failure
    (refused/reset/timed out) did NOT happen" — it does NOT mean the response
    is actually the endpoint it claims to be; see fetch_evidence_snapshot(),
    which layers a shape check on top before anything downstream trusts this
    as evidence the server process is healthy."""
    try:
        response = requests.get(
            url, headers={"Authorization": f"Bearer {api_key}"} if api_key else {}, timeout=timeout
        )
    except requests.RequestException as exc:
        return {"alive": False, "status_code": None, "body": "", "error": f"{type(exc).__name__}: {exc}"}
    return {"alive": True, "status_code": response.status_code, "body": response.text, "error": None}


def _looks_like_server_info(body: str) -> bool:
    """A genuine /get_server_info response is a JSON object carrying the
    server's own config, always including model_path. Corrected after a real
    capture (spec-07 4th real trial): a 401 body ({"error": "Unauthorized"})
    is valid JSON, is an object, and would satisfy a naive "did we get
    something parseable" check — but it is not server info. "Got a response"
    only proves the frontend answered; it does not prove the answer has the
    shape it claims to (orchestrator review, 2026-09-10: 401/403/404, or an
    HTML error page, would all pass a shape-blind check the same way)."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return False
    return isinstance(data, dict) and "model_path" in data


def _looks_like_metrics(body: str) -> bool:
    """A genuine /metrics response is Prometheus exposition text. Reuse the
    same strict sample parser the retract gate already trusts (real metric
    lines, not an error page or a JSON error blob) instead of inventing a
    second, looser heuristic for "is this actually metrics"."""
    return bool(_prometheus_samples({"metrics": {"body": body}}))


def fetch_evidence_snapshot(endpoint: str, api_key: str, timeout: float = DIAGNOSTIC_TIMEOUT_S) -> dict[str, Any]:
    """One evidence snapshot = both diagnostic endpoints, fetched independently
    (one being down doesn't hide the other). /get_server_info needs no special
    server flag; /metrics does on SGLang (--enable-metrics) but not on vLLM.

    `alive` in the returned entries is downgraded to False when a response
    DID come back (no connection-level exception) but its BODY doesn't have
    the shape that endpoint should have — a real 4th-trial capture proved
    this matters: a missing/wrong API key made every request 401, and
    "alive" was true because the frontend answered, even though the answer
    was an auth rejection, not server info (orchestrator review, 2026-09-10:
    "有回應等於活著只在回應形狀正確時才成立" — a response only proves
    liveness when its shape is right, never merely because something came
    back)."""
    base = endpoint.rstrip("/")
    server_info = fetch_endpoint_text(f"{base}/get_server_info", api_key, timeout)
    metrics = fetch_endpoint_text(f"{base}/metrics", api_key, timeout)
    if server_info["alive"] and not _looks_like_server_info(server_info["body"]):
        server_info = {**server_info, "alive": False,
                        "error": "response received but not shaped like /get_server_info (missing model_path)"}
    if metrics["alive"] and not _looks_like_metrics(metrics["body"]):
        metrics = {**metrics, "alive": False,
                   "error": "response received but not shaped like Prometheus /metrics text"}
    return {"server_info": server_info, "metrics": metrics}


_EXACT_BACKLOG_METRICS = {
    "sglang:num_retracted_reqs",
    "vllm:num_preemptions_total",
}
# 已知限制（orchestrator review 記錄，2026-09-10）：fullmatch 要求數值後面
# 直接到行尾，但 Prometheus 曝露格式允許數值後面再接一個可選的毫秒時間戳
# （"metric{labels} value timestamp"）。真的遇到那種帶時間戳的行會在這裡被
# 靜默跳過，不會誤判成別的東西——後果只是退回延遲判準（安全降級，不是答錯），
# 所以先不擋這輪；但這是刻意留下的已知限制，不是漏洞，未來若要支援帶
# 時間戳的樣本行，這裡要放寬成允許一個可選的第三個數字欄位。
_PROMETHEUS_SAMPLE_RE = re.compile(
    r"^(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)"
    r"(?P<labels>\{[^}]*\})?\s+"
    r"(?P<value>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*$"
)
_BACKLOG_NAME_RE = re.compile(r"retract|preempt", re.IGNORECASE)
_EXCLUDED_COUNTER_NAME_RE = re.compile(r"time|created|timestamp", re.IGNORECASE)


def _prometheus_samples(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse only Prometheus sample lines, skipping HELP/TYPE comments."""
    samples: list[dict[str, Any]] = []
    for endpoint_name, entry in snapshot.items():
        for raw_line in (entry.get("body") or "").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            match = _PROMETHEUS_SAMPLE_RE.fullmatch(line)
            if match is None:
                continue
            samples.append(
                {
                    "endpoint": endpoint_name,
                    "line": raw_line,
                    "metric": match.group("name"),
                    "series": match.group("name") + (match.group("labels") or ""),
                    "value": float(match.group("value")),
                }
            )
    return samples


def retract_signal_details(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return the counter and exact evidence lines used to derive it.

    Exact observed names win. Generic fallback considers only metric names
    containing retract/preempt and rejects time/created/timestamp metrics.
    No valid sample is unknown (None), so callers use the latency fallback.
    """
    samples = _prometheus_samples(snapshot)
    exact = [sample for sample in samples if sample["metric"] in _EXACT_BACKLOG_METRICS]
    if exact:
        return {"value": sum(sample["value"] for sample in exact), "match": "exact", "lines": exact}
    generic = [
        sample
        for sample in samples
        if _BACKLOG_NAME_RE.search(sample["metric"])
        and not _EXCLUDED_COUNTER_NAME_RE.search(sample["metric"])
    ]
    if generic:
        return {
            "value": sum(sample["value"] for sample in generic),
            "match": "generic_prometheus",
            "lines": generic,
        }
    return {"value": None, "match": None, "lines": []}


def retract_signal(snapshot: dict[str, Any]) -> float | None:
    """Value-only compatibility view; None means no trustworthy counter."""
    return retract_signal_details(snapshot)["value"]


def _slim_evidence(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Drop the (potentially large) raw body before a snapshot goes into the
    JSON output — the raw text is persisted separately by whoever wired
    fetch_evidence (main() writes it to sibling files); the wave record only
    needs enough to audit the decision (alive/status/error), not re-derive it."""
    return {key: {k: v for k, v in entry.items() if k != "body"} for key, entry in snapshot.items()}


def derive_timeout(
    max_tokens: int,
    wave: dict[str, Any],
    *,
    headroom: float = TIMEOUT_HEADROOM,
    latency_multiple: float = LATENCY_MULTIPLE,
) -> float:
    """Production request timeout, derived from the SELECTED wave's own
    measurements — not a fixed constant. Larger of two lower bounds:

    - latency_multiple x this wave's own worst observed latency: covers
      preemption/contention at the concurrency level actually going into
      production, so a real straggler still finishes before the timeout.
    - max_tokens / this wave's mean single-request speed / headroom: covers a
      legitimate max-length generation finishing cleanly. Once the timeout
      clears this, a runaway is bounded by max_tokens, not by the timeout —
      raising the timeout further past this point changes nothing about how
      long a stuck request can occupy a slot (see module docstring).
    """
    from_latency = latency_multiple * wave["max_latency_s"]
    from_speed = max_tokens / wave["mean_single_tok_s"] / headroom
    return max(from_latency, from_speed)


def _default_fetch_evidence() -> dict[str, Any]:
    """No-op evidence source: always "alive", never any retract signal. This
    is the default when a caller doesn't wire a real one (e.g. vLLM, or any
    test that isn't specifically exercising the liveness/retract logic) — it
    makes every wave failure look like ServerDiedError territory... except it
    can't, because reporting "alive" unconditionally means the liveness
    branch always takes the "server answered" path, never the death path.
    Silent, deliberately conservative: no evidence available reads as "can't
    prove it's dead", not as "confirmed dead"."""
    empty = {"alive": True, "status_code": None, "body": "", "error": None}
    return {"server_info": dict(empty), "metrics": dict(empty)}


class LevelResult(NamedTuple):
    """One tier's outcome, stripped down to exactly what choose_tier() needs
    to decide. Factored out (spec-09) so the selection rule can be
    calibration-tested against fixed sustained-load numbers with no HTTP and
    no timing — feed it real (n, steady_tok_per_s, safety_ok) triples and
    assert what N comes out."""

    n: int
    steady_tok_per_s: float
    safety_ok: bool


def choose_tier(levels: list[LevelResult]) -> int:
    """Pure selection logic (spec-09): given the tiers tried IN ORDER (as if
    this were the live climb), return the N to select.

    Asymmetric rule (2026-09-10 redesign; a real observed case picked N=16
    when N=32 was actually 53% faster in sustained use): climbing never
    stops just because a tier failed to set a new best — only when a tier's
    steady-state throughput falls more than SATURATION_DEGRADATION_TOLERANCE
    below the best tier seen so far, AND the very next tier ALSO fails to
    recover to within that tolerance, do we stop. The tier selected is
    always whichever had the best throughput measured overall, never simply
    the highest N or the last one tried — a real "higher N is actually
    worse" case must still be caught (that is the whole reason the old
    design compared to the *previous* tier instead of the running best).

    A tier with safety_ok=False is skipped for both the running-best
    calculation and the bad-streak bookkeeping — choose_concurrency() itself
    handles the actual fallback-on-backlog-failure return path (this
    function never sees such levels in the real pipeline, since backlog
    failure returns immediately without calling this at all; the skip here
    is defensive, not load-bearing).

    Mutation check run once by hand while implementing this (recorded here,
    not a permanent test — see spec-09 §2 and the calibration test file for
    the two things this actually proves):
    - Revert to the OLD rule ("stop at the first tier whose gain vs. the
      PREVIOUS tier is under a threshold") fed the OLD (wrong) measurement
      values (256.9, 265.7 for N=16, N=24 — the wall-clock-bound numbers)
      -> selects 16. Confirmed red against the real answer (32).
    - Same OLD rule, fed the CORRECT sustained-load values (270.5, 342.4,
      414.9) -> ALSO selects 32, same as the new rule. This is exactly why
      the primary (decision) assertion alone cannot tell the old and new
      CODE apart — the bug that actually bit real hardware was in the
      MEASUREMENT (probe_wave()'s old wall-clock-bound metric), not
      primarily in this selection rule; the calibration (magnitude) tests
      are what actually discriminate between old and new probe_wave().
    """
    if not levels:
        raise ValueError("choose_tier needs at least one level")
    best_n = levels[0].n
    best_tp = float("-inf")
    bad_streak = 0
    for level in levels:
        if not level.safety_ok:
            continue
        is_low = level.steady_tok_per_s < best_tp * (1.0 - SATURATION_DEGRADATION_TOLERANCE)
        if bad_streak >= 1 and is_low:
            break  # this tier AND the one before it both failed to recover
        if level.steady_tok_per_s > best_tp:
            best_tp = level.steady_tok_per_s
            best_n = level.n
        bad_streak = bad_streak + 1 if is_low else 0
    return best_n


def choose_concurrency(
    requests_: list[dict[str, Any]],
    run_wave: Callable[[list[dict[str, Any]], int], dict[str, Any]],
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    fetch_evidence: Callable[[], dict[str, Any]] = _default_fetch_evidence,
    candidates: tuple[int, ...] = CANDIDATES,
) -> tuple[int, list[dict[str, Any]], bool, float]:
    """Climb `candidates` (default CANDIDATES) from the smallest. Selection
    of the FINAL N is delegated to choose_tier() (see its docstring for the
    asymmetric rule) — this function's own job is running each tier's
    closed-loop wave, applying the (unchanged) safety gate, and stopping the
    live climb as soon as choose_tier() would already know the answer (no
    point spending money on more tiers once two consecutive have failed to
    recover). Returns (selected_n, waves, passed, derived_timeout_s).

    `candidates` can be a filtered subset of CANDIDATES (see main()'s
    --effective-cap): spec-07's 3rd real trial found SGLang printing its own
    hard ceiling ("max_running_requests is capped to N by the mamba state
    cache") — a candidate ABOVE that ceiling measures queuing behind the cap,
    not real concurrency, so it must never even be tried once the cap is
    known.

    `requests_` is divided into len(candidates) disjoint slices (one per
    tier, in order) so no tier's closed loop reuses another tier's prompts —
    each tier's own loop still cycles WITHIN its slice once it runs longer
    than the slice has distinct prompts (see probe_wave()); a pool sized
    with only a few repeats per tier is build_adaptive_probe_load.py's job,
    not this function's.

    Every tier is bracketed by an evidence snapshot (fetch_evidence(), before
    and after) used two ways:
    - backlog gate (UNCHANGED by the 2026-09-10 measurement/gate redesign):
      prefers whether the server's own retract signal increased during the
      wave; falls back to BACKLOG_FALLBACK_LATENCY_MULTIPLE x median latency
      only when no retract signal is available in either snapshot (see
      retract_signal()).
    - liveness: if run_wave() itself raises, the AFTER snapshot doubles as a
      liveness check. A response (any status) means the server is merely
      overloaded at this tier — a normal early stop, return normally with
      the last confirmed-safe tier. No response at all means the process is
      gone — raise ServerDiedError; the caller must NOT translate against it.
    """
    if not candidates:
        raise ValueError("candidates must be non-empty")
    if len(requests_) < len(candidates):
        raise ValueError(
            f"probe load needs at least {len(candidates)} distinct prompts "
            f"(one per candidate tier), got {len(requests_)}"
        )
    # Split PROPORTIONALLY by each candidate's own N, not evenly — a bigger
    # tier's closed loop can complete far more requests in the same T seconds
    # than a smaller tier's, so it needs a proportionally bigger disjoint
    # slice to keep repeats rare (see REQUIRED's comment: 2.5x each tier's
    # own N when the pool is exactly REQUIRED-sized). Cumulative rounding is
    # absorbed into the LAST tier so the whole pool gets used.
    total_weight = sum(candidates)
    tier_bounds: list[tuple[int, int]] = []
    cursor = 0
    for n in candidates:
        share = round(len(requests_) * n / total_weight)
        tier_bounds.append((cursor, cursor + share))
        cursor += share
    last_start, _ = tier_bounds[-1]
    tier_bounds[-1] = (last_start, len(requests_))

    waves: list[dict[str, Any]] = []
    levels: list[LevelResult] = []
    selected_n = candidates[0]
    selected_wave: dict[str, Any] | None = None
    prev_wave: dict[str, Any] | None = None
    passed_any = False
    best_tp = float("-inf")
    bad_streak = 0

    for n, (start_i, end_i) in zip(candidates, tier_bounds):
        batch = requests_[start_i:end_i]  # Every tier is cold with respect to the other tiers.
        evidence_before = fetch_evidence()
        try:
            wave = run_wave(batch, n)
        except Exception as exc:
            evidence_after = fetch_evidence()
            server_alive = bool(evidence_after["server_info"]["alive"] or evidence_after["metrics"]["alive"])
            failure_wave = {
                "n": n,
                "requests": n,
                "request_failed": True,
                "request_error": f"{type(exc).__name__}: {exc}",
                "server_alive_after_failure": server_alive,
                "evidence_before": _slim_evidence(evidence_before),
                "evidence_after": _slim_evidence(evidence_after),
                "backlog_ok": False,
                "steady_state_tok_per_s": None,
                "passed": False,
            }
            waves.append(failure_wave)
            if not server_alive:
                raise ServerDiedError(
                    f"N={n} 波失敗（{failure_wave['request_error']}），"
                    "伺服器判活（/get_server_info、/metrics）也連不上：行程可能已死，不可進翻譯",
                    waves=waves,
                ) from exc
            # Server still answers — a normal early stop (gate failed), not a
            # crash. Retreat exactly like a backlog/saturation failure would:
            # select the last confirmed-safe tier, touch nothing else.
            if selected_wave is None:
                # Even the smallest candidate couldn't complete, yet the
                # server is alive — there is no measured wave anywhere to
                # derive a timeout from. Surface this distinctly instead of
                # fabricating one; the caller falls back to its own
                # conservative default (see cloud_llm.sh's non-"ok" branch).
                raise RuntimeError(
                    f"N={n} 是最小候選也失敗，但伺服器仍活著：沒有任何一波成功量到數字，"
                    "探針無法給出安全併發或逾時"
                ) from exc
            return selected_n, waves, True, derive_timeout(max_tokens, selected_wave)

        evidence_after = fetch_evidence()
        wave["evidence_before"] = _slim_evidence(evidence_before)
        wave["evidence_after"] = _slim_evidence(evidence_after)
        wave["mean_single_tok_s"] = round(float(wave["mean_single_tok_s"]), 3)
        wave["aggregate_tok_per_s"] = round(float(wave["aggregate_tok_per_s"]), 3)
        wave["max_latency_s"] = round(float(wave["max_latency_s"]), 3)
        wave["median_latency_s"] = round(float(wave["median_latency_s"]), 3)

        retract_details_before = retract_signal_details(evidence_before)
        retract_details_after = retract_signal_details(evidence_after)
        retract_before = retract_details_before["value"]
        retract_after = retract_details_after["value"]
        wave["retract_before"] = retract_before
        wave["retract_after"] = retract_after
        # _slim_evidence intentionally drops raw bodies; retain the precise
        # parsed samples so the decision remains auditable from result JSON.
        wave["retract_evidence_before"] = retract_details_before
        wave["retract_evidence_after"] = retract_details_after
        compatible_retract_series = (
            retract_before is not None
            and retract_after is not None
            and retract_details_before["match"] == retract_details_after["match"]
            and {entry["series"] for entry in retract_details_before["lines"]}
            == {entry["series"] for entry in retract_details_after["lines"]}
        )
        wave["retract_series_compatible"] = compatible_retract_series
        if compatible_retract_series:
            wave["backlog_gate_used"] = "retract"
            backlog_ok = retract_after <= retract_before
        else:
            wave["backlog_gate_used"] = "latency_fallback"
            backlog_threshold = BACKLOG_FALLBACK_LATENCY_MULTIPLE * wave["median_latency_s"]
            wave["backlog_threshold_s"] = round(backlog_threshold, 3)
            backlog_ok = wave["max_latency_s"] <= backlog_threshold
        wave["backlog_ok"] = backlog_ok

        # 舊的飽和數字（跟前一級比）不刪除，照算照記——只是不再拿它早停（2026-09-10
        # 第二次重設計，orchestrator 指示）。真正驅動選擇的是下面的 steady_state_tok_per_s
        # 與 choose_tier() 的不對稱規則。
        if prev_wave is None:
            wave["throughput_gain"] = None
        else:
            gain = wave["aggregate_tok_per_s"] / prev_wave["aggregate_tok_per_s"] - 1.0
            wave["throughput_gain"] = round(gain, 4)
        wave["steady_state_tok_per_s"] = round(float(wave["steady_state_tok_per_s"]), 3)
        wave["passed"] = backlog_ok
        waves.append(wave)

        if not backlog_ok:
            # This tier backed up — a real straggler, not just a slow average.
            # Fall back to the best tier confirmed so far, or (if even the
            # smallest candidate backs up) to this failing tier itself.
            fallback_n = selected_n if passed_any else n
            fallback_wave = selected_wave if selected_wave is not None else wave
            return fallback_n, waves, passed_any, derive_timeout(max_tokens, fallback_wave)

        # ---- asymmetric saturation bookkeeping (see choose_tier()) ----
        current_tp = wave["steady_state_tok_per_s"]
        is_low = current_tp < best_tp * (1.0 - SATURATION_DEGRADATION_TOLERANCE)
        wave["is_low_vs_best"] = is_low
        stop_after_this = bad_streak >= 1 and is_low
        if current_tp > best_tp:
            best_tp = current_tp
            selected_n = n
            selected_wave = wave
        wave["best_steady_state_tok_per_s_so_far"] = round(best_tp, 3)
        passed_any = True
        prev_wave = wave
        levels.append(LevelResult(n=n, steady_tok_per_s=current_tp, safety_ok=True))

        if stop_after_this:
            # This tier AND the one before it both failed to recover to
            # within SATURATION_DEGRADATION_TOLERANCE of the best tier seen
            # so far — one extra tier costs about a minute, so we only give
            # up after two consecutive misses, never on the first (that is
            # exactly what made the old design pick N=16 when N=32 was 53%
            # faster). selected_wave/selected_n already point at the best
            # tier overall, not necessarily the highest N or the last tried.
            assert selected_wave is not None
            assert choose_tier(levels) == selected_n
            return selected_n, waves, True, derive_timeout(max_tokens, selected_wave)

        bad_streak = bad_streak + 1 if is_low else 0

    # Exhausted every candidate without two consecutive misses -- take
    # whichever tier had the best steady-state throughput (choose_tier()
    # would return the same answer given the same `levels`).
    assert selected_wave is not None
    assert choose_tier(levels) == selected_n
    return selected_n, waves, True, derive_timeout(max_tokens, selected_wave)


def candidates_within_cap(
    effective_cap: int | None, candidates: tuple[int, ...] = CANDIDATES
) -> tuple[int, ...]:
    """Filter `candidates` (default CANDIDATES) down to whatever a
    server-reported hard ceiling allows (spec-07, 3rd real trial: SGLang's
    own "max_running_requests is capped to N by the mamba state cache" log
    line). None means no cap was found -- use the ladder unchanged."""
    if effective_cap is None:
        return candidates
    filtered = tuple(n for n in candidates if n <= effective_cap)
    if filtered:
        return filtered
    # Even the smallest candidate exceeds the reported cap -- still try it
    # alone so a real measurement comes back instead of refusing to probe at
    # all; the caller can see candidates[0] > effective_cap in the output and
    # know the server is far more constrained than usual.
    return (candidates[0],)


def parse_candidates_override(raw: str) -> tuple[int, ...] | None:
    """Parse --candidates/PROBE_CANDIDATES ("2,4,8,16") into a strictly
    increasing tuple of positive ints, or None if not given.

    Exists so local/mechanism testing can swap the candidate ladder WITHOUT
    ever editing the tracked CANDIDATES constant. A prior round temporarily
    edited CANDIDATES directly in the working tree for a local run, marked it
    "revert before commit", and left it in place -- a second collaborator's
    independent test run against that same dirty working tree then measured
    4 failing tests and reported them as real defects in a commit that, on a
    clean checkout, actually had 2 (both pre-existing fixture gaps unrelated
    to the commit). The fix is structural, not "remember to revert": a
    source edit to a tracked file is a shared side effect the moment another
    process reads the tree, so local overrides must never touch it at all.
    """
    raw = raw.strip()
    if not raw:
        return None
    try:
        values = tuple(int(part.strip()) for part in raw.split(","))
    except ValueError as exc:
        raise ValueError(f"--candidates must be comma-separated integers, got {raw!r}") from exc
    if not values or any(v <= 0 for v in values):
        raise ValueError(f"--candidates must be positive integers, got {raw!r}")
    if list(values) != sorted(set(values)):
        raise ValueError(f"--candidates must be strictly increasing with no duplicates, got {raw!r}")
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-key", default=os.environ.get("OMLX_API_KEY", ""))
    parser.add_argument("--load", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--gpu", default="")
    parser.add_argument("--profile", default="")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_PROBE_TIMEOUT,
        help="ceiling for this probe's OWN measurement calls only — not the derived production timeout this script outputs",
    )
    parser.add_argument(
        "--effective-cap",
        type=int,
        default=None,
        help=(
            "hard concurrency ceiling the SERVER itself reported (e.g. parsed from SGLang's own "
            "'max_running_requests is capped to N by the mamba state cache' log line) — CANDIDATES "
            "above this are never tried; a candidate above a hard server-side cap measures queuing "
            "behind that cap, not real concurrency (spec-07, 3rd real trial)"
        ),
    )
    parser.add_argument(
        "--candidates",
        default=os.environ.get("PROBE_CANDIDATES", ""),
        help=(
            "override the CANDIDATES ladder for THIS run only, comma-separated ascending "
            "positive ints (e.g. '2,4,8,16') — for local/mechanism testing, so this never "
            "requires a temporary edit to the tracked CANDIDATES constant (also settable via "
            "the PROBE_CANDIDATES env var; --effective-cap still filters whichever ladder is used)"
        ),
    )
    args = parser.parse_args()

    candidates_base = parse_candidates_override(args.candidates)
    candidates = candidates_within_cap(
        args.effective_cap, candidates_base if candidates_base is not None else CANDIDATES
    )

    result: dict[str, Any] = {
        "gpu": args.gpu,
        "profile": args.profile,
        "model": args.model,
        "max_tokens": args.max_tokens,
        # spec-07 item 5 (reproducibility): this probe's OWN measurement ceiling,
        # separate from derived_timeout_s below (that one is the OUTPUT fed to
        # the real translator; this one is what bounded THIS run's own calls).
        "probe_timeout_s": args.timeout,
        "effective_cap": args.effective_cap,
        "candidates": list(candidates),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)

    def write_result() -> None:
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    evidence_calls = 0

    def real_fetch_evidence() -> dict[str, Any]:
        # spec-07 item (d)/2: raw bodies land on disk verbatim (untruncated,
        # unparsed) beside --out; the wave record in the JSON keeps only the
        # slimmed metadata (see _slim_evidence) plus these file paths, so a
        # human can go read exactly what the server said without re-running
        # anything.
        nonlocal evidence_calls
        evidence_calls += 1
        tag = f"{evidence_calls:03d}"
        snapshot = fetch_evidence_snapshot(args.endpoint, args.api_key)
        for key, entry in snapshot.items():
            out_path = args.out.parent / f"{args.out.stem}-evidence-{tag}-{key}.txt"
            out_path.write_text(entry.get("body") or "", encoding="utf-8")
            entry["file"] = str(out_path)
        return snapshot

    try:
        payload = json.loads(args.load.read_text(encoding="utf-8"))
        requests_ = [req for req in payload["requests"] if req.get("kind") == "chunk"]
        # The bundled load deliberately contains one distinct chunk per candidate slot.
        # Reusing a prompt would warm prefix cache and bias later/larger waves upward
        # (unsafe direction).
        fingerprints = {(req["system"], req["user"]) for req in requests_}
        if len(fingerprints) != len(requests_) and not payload.get("repeated_for_short_book", False):
            raise ValueError("probe load contains duplicate prompts without short-book disclosure")

        def run_wave(batch: list[dict[str, Any]], n: int) -> dict[str, Any]:
            wave = probe_wave(
                args.endpoint, args.model, args.api_key, batch, n, args.max_tokens, args.timeout
            )
            print(
                f"[adaptive-probe] N={n} duration={wave['duration_s']:.1f}s "
                f"per-request={wave['mean_single_tok_s']:.1f} tok/s "
                f"steady_state={wave['steady_state_tok_per_s']:.1f} tok/s "
                f"(aggregate_ref={wave['aggregate_tok_per_s']:.1f} tok/s) "
                f"median_latency={wave['median_latency_s']:.1f}s max_latency={wave['max_latency_s']:.1f}s",
                file=sys.stderr,
                flush=True,
            )
            return wave

        selected, waves, passed, derived_timeout = choose_concurrency(
            requests_, run_wave, max_tokens=args.max_tokens, fetch_evidence=real_fetch_evidence,
            candidates=candidates,
        )
        result.update(
            status="ok",
            selected_concurrency=selected,
            passed=passed,
            waves=waves,
            derived_timeout_s=round(derived_timeout, 3),
        )
        write_result()
        return 0
    except ServerDiedError as exc:
        # Distinct from generic "error" on purpose (spec-07 item 1): the
        # caller (cloud_llm.sh) must refuse to fall back to a default
        # concurrency and translate anyway when THIS status comes back —
        # that fallback is exactly how the first real trial ran 120 prompts
        # against an already-dead server.
        result.update(status="server_died", error=str(exc), waves=exc.waves)
        write_result()
        print(f"[adaptive-probe] server died: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        result.update(status="error", error=f"{type(exc).__name__}: {exc}")
        write_result()
        print(f"[adaptive-probe] failed: {result['error']}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

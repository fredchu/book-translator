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
  - saturation: this wave's AGGREGATE (whole-wave) throughput must beat the
    previous (smaller) tier by at least SATURATION_GAIN (catches "bigger N
    stopped helping"). Correction after initial review: this must be
    aggregate throughput, not mean per-request speed — per-request speed
    falls as N rises even when the server has plenty of headroom left, so
    gating on it made saturation_ok go negative on the very first climb and
    froze selection at the smallest candidate regardless of real capacity.
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
   retract-shaped line can be found in either diagnostic endpoint's raw text.
   The field/endpoint that actually carries a retract counter on a given
   SGLang build has never been directly observed, so nothing pins one: both
   endpoints' raw bodies are scanned for any line containing "retract" and
   every number on it is summed, per spec-07 review ("machine_id 那次是運氣
   好，這次不要賭" — getting away with guessing a field name once is not a
   reason to do it again).
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from providers.omlx_provider import DEFAULT_MAX_TOKENS  # noqa: E402  single source with the translator

CANDIDATES = (8, 12, 16, 24, 32)
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
SATURATION_GAIN = 0.10
DIAGNOSTIC_TIMEOUT_S = 10.0
_RETRACT_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


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
    barrier: threading.Barrier,
) -> dict[str, float]:
    barrier.wait(timeout=10)
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
) -> dict[str, Any]:
    if len(requests_) != n:
        raise ValueError(f"N={n} requires exactly {n} prompts, got {len(requests_)}")
    barrier = threading.Barrier(n)
    with cf.ThreadPoolExecutor(max_workers=n) as pool:
        futures = [
            pool.submit(probe_one, endpoint, model, api_key, req, max_tokens, timeout, barrier)
            for req in requests_
        ]
        rows = [future.result() for future in futures]
    latencies = [row["elapsed_s"] for row in rows]
    wall_clock_s = max(latencies)
    total_out_tokens = sum(row["out_tokens"] for row in rows)
    return {
        "n": n,
        "requests": n,
        # Per-REQUEST speed. Naturally DECREASES as concurrency rises (more
        # requests sharing the same GPU/scheduler) even when the server is
        # doing strictly more total work — never use this to judge whether
        # raising N is still paying off (see choose_concurrency()).
        "mean_single_tok_s": statistics.mean(row["tok_per_s"] for row in rows),
        # Whole-WAVE throughput: all N requests' output tokens over the wall
        # time the wave actually took (barrier-started, ends when the last
        # one finishes). This is what "is bigger N still helping" means.
        "aggregate_tok_per_s": total_out_tokens / wall_clock_s,
        "max_latency_s": wall_clock_s,
        "median_latency_s": statistics.median(latencies),
        # Keep raw measurements in telemetry so aggregation can be audited without
        # asserting anything about the runner's absolute wall-clock performance.
        "samples": rows,
    }


def fetch_endpoint_text(url: str, api_key: str, timeout: float = DIAGNOSTIC_TIMEOUT_S) -> dict[str, Any]:
    """GET a diagnostic endpoint and keep its raw body verbatim — used both as
    the post-failure liveness check and as before/after evidence for the
    backlog gate. Getting ANY response (any status code) means the HTTP
    frontend process is still alive; only a connection-level failure
    (refused/reset/timed out) means it is not — a 500 with a body is a
    process that is up and complaining, not a dead one (spec-07 review)."""
    try:
        response = requests.get(
            url, headers={"Authorization": f"Bearer {api_key}"} if api_key else {}, timeout=timeout
        )
    except requests.RequestException as exc:
        return {"alive": False, "status_code": None, "body": "", "error": f"{type(exc).__name__}: {exc}"}
    return {"alive": True, "status_code": response.status_code, "body": response.text, "error": None}


def fetch_evidence_snapshot(endpoint: str, api_key: str, timeout: float = DIAGNOSTIC_TIMEOUT_S) -> dict[str, Any]:
    """One evidence snapshot = both diagnostic endpoints, fetched independently
    (one being down doesn't hide the other). /get_server_info needs no special
    server flag; /metrics does on SGLang (--enable-metrics) but not on vLLM."""
    base = endpoint.rstrip("/")
    return {
        "server_info": fetch_endpoint_text(f"{base}/get_server_info", api_key, timeout),
        "metrics": fetch_endpoint_text(f"{base}/metrics", api_key, timeout),
    }


def _retract_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if "retract" in line.lower()]


def retract_signal(snapshot: dict[str, Any]) -> float | None:
    """Sum every number on every line mentioning "retract" across BOTH
    endpoints in one snapshot. Returns None — never 0 — when no such line
    exists anywhere: we have never directly observed which endpoint or field
    name actually carries a retract counter on a given SGLang build, so
    finding nothing means "unknown", not "zero retracts happened". The
    caller (choose_concurrency()) must fall back to the latency gate on
    None, not treat it as a clean bill of health (spec-07 review: "machine_id
    那次是運氣好，這次不要賭" — don't pin a field nobody has actually seen)."""
    found = False
    total = 0.0
    for entry in snapshot.values():
        for line in _retract_lines(entry.get("body") or ""):
            found = True
            total += sum(float(m) for m in _RETRACT_NUMBER_RE.findall(line))
    return total if found else None


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


def choose_concurrency(
    requests_: list[dict[str, Any]],
    run_wave: Callable[[list[dict[str, Any]], int], dict[str, Any]],
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    fetch_evidence: Callable[[], dict[str, Any]] = _default_fetch_evidence,
) -> tuple[int, list[dict[str, Any]], bool, float]:
    """Climb CANDIDATES from the smallest, stopping at the first tier that
    either backs up (a real straggler, not just an average) or stops paying
    off (throughput gain under SATURATION_GAIN over the previous tier).
    Returns (selected_n, waves, passed, derived_timeout_s).

    Every tier is bracketed by an evidence snapshot (fetch_evidence(), before
    and after) used two ways:
    - backlog gate: prefers whether the server's own retract signal increased
      during the wave; falls back to BACKLOG_FALLBACK_LATENCY_MULTIPLE x
      median latency only when no retract signal is available in either
      snapshot (see retract_signal()).
    - liveness: if run_wave() itself raises, the AFTER snapshot doubles as a
      liveness check. A response (any status) means the server is merely
      overloaded at this tier — a normal early stop, return normally with
      the last confirmed-safe tier. No response at all means the process is
      gone — raise ServerDiedError; the caller must NOT translate against it.
    """
    required = sum(CANDIDATES)
    if len(requests_) < required:
        raise ValueError(f"probe load needs {required} distinct large chunks, got {len(requests_)}")

    offset = 0
    waves: list[dict[str, Any]] = []
    selected_n = CANDIDATES[0]
    selected_wave: dict[str, Any] | None = None
    prev_wave: dict[str, Any] | None = None
    passed_any = False

    for n in CANDIDATES:
        batch = requests_[offset : offset + n]
        offset += n  # Every wave is cold with respect to earlier prompts.
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
                "saturation_ok": None,
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

        retract_before = retract_signal(evidence_before)
        retract_after = retract_signal(evidence_after)
        wave["retract_before"] = retract_before
        wave["retract_after"] = retract_after
        if retract_before is not None and retract_after is not None:
            wave["backlog_gate_used"] = "retract"
            backlog_ok = retract_after <= retract_before
        else:
            wave["backlog_gate_used"] = "latency_fallback"
            backlog_threshold = BACKLOG_FALLBACK_LATENCY_MULTIPLE * wave["median_latency_s"]
            wave["backlog_threshold_s"] = round(backlog_threshold, 3)
            backlog_ok = wave["max_latency_s"] <= backlog_threshold
        wave["backlog_ok"] = backlog_ok

        if prev_wave is None:
            saturation_ok = True
            wave["throughput_gain"] = None
        else:
            # Gate on AGGREGATE (whole-wave) throughput, not mean_single_tok_s.
            # Per-request speed falls as N rises even on a server with plenty
            # of headroom left — gating on it would make saturation_ok go
            # negative on the very first step and freeze selection at the
            # smallest candidate forever, regardless of real capacity.
            gain = wave["aggregate_tok_per_s"] / prev_wave["aggregate_tok_per_s"] - 1.0
            wave["throughput_gain"] = round(gain, 4)
            saturation_ok = gain >= SATURATION_GAIN
        wave["saturation_ok"] = saturation_ok
        wave["passed"] = backlog_ok and saturation_ok
        waves.append(wave)

        if not backlog_ok:
            # This tier backed up — a real straggler, not just a slow average.
            # Fall back to the last tier that was actually safe, or (if even
            # the smallest candidate backs up) to this failing tier itself.
            fallback_n = selected_n if passed_any else n
            fallback_wave = selected_wave if selected_wave is not None else wave
            return fallback_n, waves, passed_any, derive_timeout(max_tokens, fallback_wave)
        if not saturation_ok:
            # Safe, but climbing further stopped paying off — the previous
            # tier (already confirmed safe) is the practical ceiling.
            assert selected_wave is not None  # prev_wave set => a prior tier passed
            return selected_n, waves, True, derive_timeout(max_tokens, selected_wave)

        selected_n = n
        selected_wave = wave
        passed_any = True
        prev_wave = wave

    # Every candidate cleared both gates — take the top of the range.
    assert selected_wave is not None
    return selected_n, waves, True, derive_timeout(max_tokens, selected_wave)


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
    args = parser.parse_args()

    result: dict[str, Any] = {
        "gpu": args.gpu,
        "profile": args.profile,
        "model": args.model,
        "max_tokens": args.max_tokens,
        # spec-07 item 5 (reproducibility): this probe's OWN measurement ceiling,
        # separate from derived_timeout_s below (that one is the OUTPUT fed to
        # the real translator; this one is what bounded THIS run's own calls).
        "probe_timeout_s": args.timeout,
        "candidates": list(CANDIDATES),
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
                f"[adaptive-probe] N={n} per-request={wave['mean_single_tok_s']:.1f} tok/s "
                f"aggregate={wave['aggregate_tok_per_s']:.1f} tok/s "
                f"median_latency={wave['median_latency_s']:.1f}s max_latency={wave['max_latency_s']:.1f}s",
                file=sys.stderr,
                flush=True,
            )
            return wave

        selected, waves, passed, derived_timeout = choose_concurrency(
            requests_, run_wave, max_tokens=args.max_tokens, fetch_evidence=real_fetch_evidence
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

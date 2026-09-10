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
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
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
LATENCY_MULTIPLE = 3.0
SATURATION_GAIN = 0.10


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
    response.raise_for_status()
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


def choose_concurrency(
    requests_: list[dict[str, Any]],
    run_wave: Callable[[list[dict[str, Any]], int], dict[str, Any]],
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> tuple[int, list[dict[str, Any]], bool, float]:
    """Climb CANDIDATES from the smallest, stopping at the first tier that
    either backs up (a real straggler, not just an average) or stops paying
    off (throughput gain under SATURATION_GAIN over the previous tier).
    Returns (selected_n, waves, passed, derived_timeout_s).
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
        wave = run_wave(batch, n)
        wave["mean_single_tok_s"] = round(float(wave["mean_single_tok_s"]), 3)
        wave["aggregate_tok_per_s"] = round(float(wave["aggregate_tok_per_s"]), 3)
        wave["max_latency_s"] = round(float(wave["max_latency_s"]), 3)
        wave["median_latency_s"] = round(float(wave["median_latency_s"]), 3)

        backlog_threshold = LATENCY_MULTIPLE * wave["median_latency_s"]
        backlog_ok = wave["max_latency_s"] <= backlog_threshold
        wave["backlog_threshold_s"] = round(backlog_threshold, 3)
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
        "candidates": list(CANDIDATES),
    }
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
            requests_, run_wave, max_tokens=args.max_tokens
        )
        result.update(
            status="ok",
            selected_concurrency=selected,
            passed=passed,
            waves=waves,
            derived_timeout_s=round(derived_timeout, 3),
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    except Exception as exc:
        result.update(status="error", error=f"{type(exc).__name__}: {exc}")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[adaptive-probe] failed: {result['error']}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

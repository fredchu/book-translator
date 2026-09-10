#!/usr/bin/env python3
"""Measure a server's safe client concurrency with disjoint, production-sized prompts."""

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

CANDIDATES = (24, 16, 12, 8)
DEFAULT_MAX_TOKENS = 2048
DEFAULT_TIMEOUT = 120.0
TIMEOUT_HEADROOM = 0.8
MAX_LATENCY_FRACTION = 0.5


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
    return {
        "n": n,
        "requests": n,
        "mean_single_tok_s": statistics.mean(row["tok_per_s"] for row in rows),
        "max_latency_s": max(row["elapsed_s"] for row in rows),
        # Keep raw measurements in telemetry so aggregation can be audited without
        # asserting anything about the runner's absolute wall-clock performance.
        "samples": rows,
    }


def choose_concurrency(
    requests_: list[dict[str, Any]],
    run_wave: Callable[[list[dict[str, Any]], int], dict[str, Any]],
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[int, list[dict[str, Any]], bool]:
    required = sum(CANDIDATES)
    if len(requests_) < required:
        raise ValueError(f"probe load needs {required} distinct large chunks, got {len(requests_)}")
    threshold = max_tokens / (timeout * TIMEOUT_HEADROOM)
    max_latency = timeout * MAX_LATENCY_FRACTION
    offset = 0
    waves: list[dict[str, Any]] = []
    for n in CANDIDATES:
        batch = requests_[offset : offset + n]
        offset += n  # Every wave is cold with respect to earlier prompts.
        wave = run_wave(batch, n)
        wave["mean_single_tok_s"] = round(float(wave["mean_single_tok_s"]), 3)
        wave["max_latency_s"] = round(float(wave["max_latency_s"]), 3)
        wave["speed_threshold_tok_s"] = round(threshold, 3)
        wave["max_allowed_latency_s"] = round(max_latency, 3)
        wave["passed"] = (
            wave["mean_single_tok_s"] >= threshold
            and wave["max_latency_s"] < max_latency
        )
        waves.append(wave)
        if wave["passed"]:
            return n, waves, True
    return CANDIDATES[-1], waves, False


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
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()

    result: dict[str, Any] = {
        "gpu": args.gpu,
        "profile": args.profile,
        "model": args.model,
        "max_tokens": args.max_tokens,
        "timeout_s": args.timeout,
        "timeout_headroom": TIMEOUT_HEADROOM,
        "candidates": list(CANDIDATES),
    }
    try:
        payload = json.loads(args.load.read_text(encoding="utf-8"))
        requests_ = [req for req in payload["requests"] if req.get("kind") == "chunk"]
        # The bundled load deliberately contains 60 different full-sized chunks. Reusing a
        # prompt would warm prefix cache and bias later/lower waves upward (unsafe direction).
        fingerprints = {(req["system"], req["user"]) for req in requests_}
        if len(fingerprints) != len(requests_) and not payload.get("repeated_for_short_book", False):
            raise ValueError("probe load contains duplicate prompts without short-book disclosure")

        def run_wave(batch: list[dict[str, Any]], n: int) -> dict[str, Any]:
            wave = probe_wave(
                args.endpoint, args.model, args.api_key, batch, n, args.max_tokens, args.timeout
            )
            print(
                f"[adaptive-probe] N={n} mean={wave['mean_single_tok_s']:.1f} tok/s "
                f"max_latency={wave['max_latency_s']:.1f}s",
                file=sys.stderr,
                flush=True,
            )
            return wave

        selected, waves, passed = choose_concurrency(
            requests_, run_wave, max_tokens=args.max_tokens, timeout=args.timeout
        )
        result.update(status="ok", selected_concurrency=selected, passed=passed, waves=waves)
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

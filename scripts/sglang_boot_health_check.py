#!/usr/bin/env python3
"""Read-only SGLang boot health check (spec-07, review-mem-budget.md §8.6).

Classifies six specific boot-log lines into PASS/MARGINAL/FAIL/MISSING per
row, and rolls them up into one overall PASS/FAIL/UNKNOWN verdict. This
script NEVER terminates anything and never should — three real SGLang fp8
trials have already been misjudged by a human eyeballing this same log
(a caught, already-handled libtorchcodec import exception near the top of
every boot log looks fatal but isn't; the "capped to" line's ABSENCE is a
PASS condition here, not a parse failure). Replacing that eyeballing with a
script that has never been tested against a real failing boot and letting
IT decide whether to kill a machine that took 20+ minutes to boot would be
a worse risk than the human misreads it exists to fix — so it only reports;
whoever calls it decides whether to terminate.

Exit codes (see VERDICT_EXIT_CODES):
  0 = PASS    — all six criteria clear (some may be MARGINAL; none FAIL/MISSING)
  1 = FAIL    — at least one criterion crossed its kill line
  3 = UNKNOWN — not enough of the six lines have appeared to judge yet.
                A missing line is treated as "don't know", never as "fine" —
                that silent-failure shape is exactly what burned the first
                three trials (a probe that "didn't find" a signal was read
                as "nothing wrong" instead of "couldn't tell").

Deliberately NOT 2: argparse itself always exits 2 on its own usage errors
(e.g. --log omitted) — a real mistake made once, live, while deciding
whether to kill a machine mid-boot. Reading that "2" as "ran fine, just not
enough info yet" instead of "never ran at all" would cost real minutes on a
machine burning money every one of them. UNKNOWN must never share a code
with "the script didn't run" — see test_missing_log_argument_exits_with_....
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import NamedTuple

MAMBA_CACHE_RE = re.compile(r"Mamba Cache is allocated\.\s*max_mamba_cache_size:\s*(\d+)")
CAPPED_TO_RE = re.compile(r"max_running_requests is capped to (\d+)")
READY_SUMMARY_RE = re.compile(r"max_total_num_tokens=(\d+).*?max_running_requests=(\d+)")
MEMORY_POOL_END_RE = re.compile(r"Memory pool end\.\s*avail mem=([\d.]+)\s*GB")
DECODE_GRAPH_END_RE = re.compile(r"Capture target decode CUDA graph end.*?avail mem=([\d.]+)\s*GB")
FAILURE_MARKER_RE = re.compile(r"Scheduler hit an exception|OutOfMemoryError")

STATUS_ORDER = ("PASS", "MARGINAL", "FAIL", "MISSING")
# 2 is deliberately absent: argparse's own parsing errors (e.g. forgetting
# --log) always exit 2, and that must read as "never ran", never as
# "ran, insufficient info" -- a real mistake made once while deciding
# whether to kill a machine mid-boot (orchestrator review, 2026-09-10).
VERDICT_EXIT_CODES = {"PASS": 0, "FAIL": 1, "UNKNOWN": 3}


class RowResult(NamedTuple):
    label: str
    status: str  # one of STATUS_ORDER
    detail: str  # the raw matched text (or an explanation), for human cross-check


def _find_last(pattern: re.Pattern[str], lines: list[str]) -> tuple[int, re.Match[str]] | None:
    """Last match wins — a restarted container can print these lines more
    than once; only the final boot attempt's numbers are the real answer."""
    found: tuple[int, re.Match[str]] | None = None
    for i, line in enumerate(lines):
        m = pattern.search(line)
        if m:
            found = (i, m)
    return found


def _bucket(value: float, pass_at: float, fail_below: float) -> str:
    if value < fail_below:
        return "FAIL"
    if value >= pass_at:
        return "PASS"
    return "MARGINAL"


def check_mamba_cache_size(lines: list[str]) -> RowResult:
    """review-mem-budget.md §8.6 訂正：allocator 的可用槽是 arange(1, size+1)
    （槽 0 是佔位不算），size=32 時可用正好 32 槽——池子張量雖是 size+1 列，
    但 log 印的 max_mamba_cache_size 印的是 size 本身。過關值原本寫「32 或
    33」，訂正後嚴格只認 32，33 拿掉。"""
    hit = _find_last(MAMBA_CACHE_RE, lines)
    if hit is None:
        return RowResult("1 Mamba Cache 槽數 (max_mamba_cache_size)", "MISSING",
                          "找不到 'Mamba Cache is allocated' 那行")
    _, m = hit
    x = int(m.group(1))
    return RowResult("1 Mamba Cache 槽數 (max_mamba_cache_size)",
                      "PASS" if x == 32 else "FAIL", m.group(0))


def check_capped_to(lines: list[str]) -> RowResult:
    hit = _find_last(CAPPED_TO_RE, lines)
    if hit is None:
        # Absence is the PASS shape here (disabling radix cache makes the
        # mamba pool size equal --max-running-requests directly, so SGLang
        # never prints this line at all — see review-mem-budget.md §1) —
        # but that reading assumes the log is complete enough to have
        # reached this point. A truncated (still-booting) log also lacks
        # this line for an unrelated reason; the OTHER rows' MISSING status
        # is what correctly drives the overall verdict to UNKNOWN in that
        # case, not this row on its own.
        return RowResult("2 max_running_requests is capped to N", "PASS", "沒有這行（沒被夾住）")
    _, m = hit
    n = int(m.group(1))
    return RowResult("2 max_running_requests is capped to N",
                      "PASS" if n >= 16 else "FAIL", m.group(0))


def check_ready_summary(lines: list[str]) -> tuple[RowResult, RowResult]:
    hit = _find_last(READY_SUMMARY_RE, lines)
    if hit is None:
        return (
            RowResult("3 就緒摘要 max_running_requests(M)", "MISSING", "找不到就緒摘要那行"),
            RowResult("3b 就緒摘要 max_total_num_tokens(T)", "MISSING", "找不到就緒摘要那行"),
        )
    _, m = hit
    t, running_m = int(m.group(1)), int(m.group(2))
    m_status = "PASS" if running_m == 32 else ("FAIL" if running_m <= 15 else "MARGINAL")
    t_status = _bucket(t, pass_at=118_000, fail_below=100_000)
    return (
        RowResult("3 就緒摘要 max_running_requests(M)", m_status, m.group(0)),
        RowResult("3b 就緒摘要 max_total_num_tokens(T)", t_status, m.group(0)),
    )


def extract_max_total_num_tokens(text: str) -> int | None:
    """Raw extraction only, no PASS/FAIL/MARGINAL judgment (2026-09-10, 5th
    real trial) -- for callers that need the NUMBER itself, not a verdict
    about it (adaptive_concurrency_probe.py's --max-total-num-tokens derives
    the KV-cache candidate ceiling from this). None if the line never
    appeared; the caller decides what "unknown" means for its own purpose,
    same discipline as the rest of this file."""
    hit = _find_last(READY_SUMMARY_RE, text.splitlines())
    if hit is None:
        return None
    _, m = hit
    return int(m.group(1))


def check_memory_pool_end(lines: list[str]) -> RowResult:
    hit = _find_last(MEMORY_POOL_END_RE, lines)
    if hit is None:
        return RowResult("4 Memory pool end", "MISSING", "找不到這行")
    _, m = hit
    return RowResult("4 Memory pool end", _bucket(float(m.group(1)), pass_at=5.0, fail_below=4.0), m.group(0))


def check_decode_graph_end(lines: list[str]) -> tuple[RowResult, int | None]:
    hit = _find_last(DECODE_GRAPH_END_RE, lines)
    if hit is None:
        return RowResult("5 decode CUDA graph end", "MISSING", "找不到這行"), None
    idx, m = hit
    return RowResult("5 decode CUDA graph end", _bucket(float(m.group(1)), pass_at=2.5, fail_below=2.0), m.group(0)), idx


def check_failure_marker(lines: list[str], after_index: int | None) -> RowResult:
    """Only counts a hit AFTER row 5's own line (by line order) — so an
    earlier, already-handled exception (e.g. the libtorchcodec optional-
    dependency traceback near the top of every real boot log, bracketed by
    its own "[start/end of ... traceback]" markers) is never mistaken for
    the real failure. If row 5 itself is missing, the whole log is scanned
    (there is no known-good anchor point yet)."""
    start = 0 if after_index is None else after_index + 1
    for i in range(start, len(lines)):
        if FAILURE_MARKER_RE.search(lines[i]):
            return RowResult("6 崩潰標記（在第 5 行之後）", "FAIL", f"第 {i + 1} 行：{lines[i].strip()[:200]}")
    return RowResult("6 崩潰標記（在第 5 行之後）", "PASS", "沒有出現")


def run_health_check(text: str) -> tuple[list[RowResult], str]:
    lines = text.splitlines()
    row1 = check_mamba_cache_size(lines)
    row2 = check_capped_to(lines)
    row3, row3b = check_ready_summary(lines)
    row4 = check_memory_pool_end(lines)
    row5, row5_idx = check_decode_graph_end(lines)
    row6 = check_failure_marker(lines, row5_idx)
    rows = [row1, row2, row3, row3b, row4, row5, row6]

    if any(r.status == "FAIL" for r in rows):
        verdict = "FAIL"
    elif any(r.status == "MISSING" for r in rows):
        verdict = "UNKNOWN"
    else:
        verdict = "PASS"
    return rows, verdict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", type=Path, required=True, help="path to the container's boot log text")
    parser.add_argument(
        "--print-max-total-num-tokens",
        action="store_true",
        help=(
            "print only the ready-summary's max_total_num_tokens value (5th real trial: feeds "
            "adaptive_concurrency_probe.py's --max-total-num-tokens); does NOT run the six-row "
            "health check and returns plain 0/1 (found/not found), never VERDICT_EXIT_CODES, so "
            "this mode's exit code can never be confused with a PASS/FAIL/UNKNOWN verdict"
        ),
    )
    args = parser.parse_args(argv)
    text = args.log.read_text(encoding="utf-8", errors="replace")

    if args.print_max_total_num_tokens:
        value = extract_max_total_num_tokens(text)
        if value is None:
            return 1
        print(value)
        return 0

    rows, verdict = run_health_check(text)
    for row in rows:
        print(f"[{row.status:8s}] {row.label}：{row.detail}")
    print(f"總結：{verdict}")
    return VERDICT_EXIT_CODES[verdict]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

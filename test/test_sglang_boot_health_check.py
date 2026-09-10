"""sglang_boot_health_check.py：唯讀判讀，三條路徑都要測到（PASS／FAIL／UNKNOWN）。

腳本本身絕不可以呼叫任何砍機動作——這裡守的是「六行的實測值都印出來給人核對，
缺行算 UNKNOWN 不算 PASS」，不是砍機邏輯本身（那個決定權在人）。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO / "scripts" / "sglang_boot_health_check.py"
spec = importlib.util.spec_from_file_location("sglang_boot_health_check", MODULE_PATH)
assert spec and spec.loader
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

REAL_TRIAL3_LOG = (REPO / "test" / "fixtures" / "sglang-fp8-trial3-boot-log.txt").read_text(
    encoding="utf-8", errors="replace"
)


def test_never_imports_or_references_any_termination_call() -> None:
    """Structural guard: this module must stay read-only. Grep its own
    source for anything that could kill a machine — vastai/runpod CLI
    invocation, subprocess calls, os.system, etc."""
    source = MODULE_PATH.read_text(encoding="utf-8")
    for forbidden in ("import subprocess", "os.system", "vastai", "vast_lib_", "runpod_lib_", "os.kill"):
        assert forbidden not in source, f"health check script must stay read-only, found {forbidden!r}"


def test_real_trial3_log_is_fail_and_names_the_rows_that_tripped() -> None:
    """The exact real capture that burned trial 3 — must classify FAIL and
    explicitly name at least the two rows the user called out: the 'capped
    to' line (9 <= 15) and Memory pool end (3.81 < 4.0). It also actually
    trips row 1 (mamba cache size 47, not 32), row 3 (M=9), row 5 (1.35 <
    2.0), and row 6 (the real OOM traceback) — assert those too, since a
    check that only caught the two named ones while missing the rest would
    still be an under-built health check."""
    rows, verdict = check.run_health_check(REAL_TRIAL3_LOG)
    assert verdict == "FAIL"
    by_label = {row.label: row for row in rows}

    mamba_row = next(r for r in rows if r.label.startswith("1 "))
    assert mamba_row.status == "FAIL"
    assert "47" in mamba_row.detail

    capped_row = next(r for r in rows if r.label.startswith("2 "))
    assert capped_row.status == "FAIL"
    assert "9" in capped_row.detail

    m_row = next(r for r in rows if r.label.startswith("3 "))
    assert m_row.status == "FAIL"

    pool_row = next(r for r in rows if r.label.startswith("4 "))
    assert pool_row.status == "FAIL"
    assert "3.81" in pool_row.detail

    graph_row = next(r for r in rows if r.label.startswith("5 "))
    assert graph_row.status == "FAIL"
    assert "1.35" in graph_row.detail

    crash_row = next(r for r in rows if r.label.startswith("6 "))
    assert crash_row.status == "FAIL"
    assert "OutOfMemoryError" in crash_row.detail or "Scheduler hit an exception" in crash_row.detail
    del by_label  # kept for readability of the lookups above


def test_real_trial3_log_row6_ignores_the_earlier_handled_libtorchcodec_traceback() -> None:
    """spec-07's own 'read early' trap, reproduced from the real log: a
    caught, already-handled libtorchcodec import exception sits near the
    TOP of this exact file, long before row 5's line. Row 6 must anchor on
    row 5's position and report the line number of the REAL crash (deep in
    the 600s), not accidentally key off that earlier, irrelevant text."""
    rows, _verdict = check.run_health_check(REAL_TRIAL3_LOG)
    crash_row = next(r for r in rows if r.label.startswith("6 "))
    # The real crash is reported near line 673 (1-indexed) in the fixture;
    # assert it's nowhere near the libtorchcodec section (line ~18-197).
    line_no = int(crash_row.detail.split("第 ")[1].split(" 行")[0])
    assert line_no > 600


def test_failure_marker_before_row5_is_ignored_not_flagged() -> None:
    """The position gate itself, isolated from the real fixture's specific
    content (which happens not to contain the marker text before row 5):
    an exception marker appearing BEFORE row 5's line must not trip row 6 —
    only one appearing after it counts. Without this gate, an early
    unrelated exception (any caught-and-handled one, not just the real
    libtorchcodec case) would falsely flag a healthy boot as FAIL."""
    log = "\n".join([
        "Scheduler hit an exception: (caught elsewhere, already handled, irrelevant)",
        "Mamba Cache is allocated. max_mamba_cache_size: 32, conv_state size: 0.09GB, ssm_state size: 4.62GB",
        "Memory pool end. avail mem=5.70 GB",
        "Capture target decode CUDA graph end. elapsed=3.8 s, mem usage=0.1 GB, avail mem=3.10 GB.",
        "max_total_num_tokens=128000, max_running_requests=32, context_len=16384",
    ])
    rows, verdict = check.run_health_check(log)
    crash_row = next(r for r in rows if r.label.startswith("6 "))
    assert crash_row.status == "PASS"
    assert verdict == "PASS"


def _synthetic_pass_log() -> str:
    return "\n".join([
        "[2026-09-11 00:00:00] Load weight begin. avail mem=46.86 GB",
        "[2026-09-11 00:00:44] Load weight end. elapsed=44.01 s, avail mem=18.40 GB, mem usage=28.47 GB.",
        "[2026-09-11 00:00:50] Mamba Cache is allocated. max_mamba_cache_size: 32, conv_state size: 0.09GB, ssm_state size: 4.62GB",
        "[2026-09-11 00:00:50] KV Cache is allocated. dtype: torch.bfloat16, #tokens: 128000, K size: 3.90 GB, V size: 3.90 GB",
        "[2026-09-11 00:00:50] Memory pool end. avail mem=5.70 GB",
        "[2026-09-11 00:01:50] Capture target prefill CUDA graph end. elapsed=59.0 s, mem usage=1.9 GB, avail mem=3.8 GB.",
        "[2026-09-11 00:01:54] Capture target decode CUDA graph end. elapsed=3.8 s, mem usage=0.1 GB, avail mem=3.10 GB.",
        "[2026-09-11 00:01:54] max_total_num_tokens=128000, chunked_prefill_size=4096, max_prefill_tokens=16384, max_running_requests=32, context_len=16384, available_gpu_mem=3.10 GB",
        "[2026-09-11 00:01:55] INFO:     Application startup complete.",
    ])


def test_synthetic_success_shape_is_pass() -> None:
    rows, verdict = check.run_health_check(_synthetic_pass_log())
    assert verdict == "PASS"
    assert all(row.status in ("PASS", "MARGINAL") for row in rows)


def test_synthetic_success_shape_row1_requires_exactly_32_not_33() -> None:
    """review-mem-budget.md §8.6 訂正：allocator 槽 0 是佔位不算，size=32 時
    可用正好 32 槽，過關值嚴格等於 32——33 不再算過。"""
    log = _synthetic_pass_log().replace("max_mamba_cache_size: 32", "max_mamba_cache_size: 33")
    rows, verdict = check.run_health_check(log)
    mamba_row = next(r for r in rows if r.label.startswith("1 "))
    assert mamba_row.status == "FAIL"
    assert verdict == "FAIL"


def _truncated_log() -> str:
    # Only through "Load weight begin" -- boot still in progress, none of
    # the six rows' lines have appeared yet (row 2's absence-is-PASS
    # shortcut doesn't matter here: the other MISSING rows still drive the
    # overall verdict to UNKNOWN).
    lines = REAL_TRIAL3_LOG.splitlines()
    cutoff = next(i for i, line in enumerate(lines) if "Load weight begin" in line) + 1
    return "\n".join(lines[:cutoff])


def test_truncated_log_is_unknown_not_pass() -> None:
    """A log cut off mid-boot must never read as PASS just because nothing
    has failed yet -- that is exactly the silent-failure shape spec-07
    exists to catch (a missing signal read as "nothing wrong")."""
    rows, verdict = check.run_health_check(_truncated_log())
    assert verdict == "UNKNOWN"
    missing_labels = {row.label for row in rows if row.status == "MISSING"}
    assert any(label.startswith("1 ") for label in missing_labels)
    assert any(label.startswith("3 ") for label in missing_labels)
    assert any(label.startswith("4 ") for label in missing_labels)
    assert any(label.startswith("5 ") for label in missing_labels)


def test_truncated_log_never_reports_fail() -> None:
    """Distinguish the two 'not fine' verdicts: a truncated log with no
    failure marker yet must be UNKNOWN, not FAIL -- conflating the two
    would make an operator kill a machine that's simply still booting."""
    _rows, verdict = check.run_health_check(_truncated_log())
    assert verdict != "FAIL"


@pytest.mark.parametrize(
    ("verdict", "expected_code"),
    [("PASS", 0), ("FAIL", 1), ("UNKNOWN", 3)],
)
def test_main_exit_codes_match_verdict(tmp_path: Path, verdict: str, expected_code: int) -> None:
    log_text = {
        "PASS": _synthetic_pass_log(),
        "FAIL": REAL_TRIAL3_LOG,
        "UNKNOWN": _truncated_log(),
    }[verdict]
    log_path = tmp_path / "boot.log"
    log_path.write_text(log_text, encoding="utf-8")
    rc = check.main(["--log", str(log_path)])
    assert rc == expected_code


def test_unknown_exit_code_never_collides_with_argparses_own_error_code() -> None:
    """argparse always exits 2 on its own parsing errors -- that must mean
    "the script never ran", never "ran, insufficient info". Real mistake
    made once live (orchestrator forgot --log while deciding whether to
    kill a machine mid-boot, saw exit=2, briefly read it as UNKNOWN)."""
    assert check.VERDICT_EXIT_CODES["UNKNOWN"] != 2
    assert check.VERDICT_EXIT_CODES["FAIL"] != 2
    assert check.VERDICT_EXIT_CODES["PASS"] != 2


def test_missing_log_argument_exits_with_argparses_own_code_not_unknown() -> None:
    """Reproduces the real mistake directly: invoking the script without
    --log must fail with argparse's own usage-error code (2) -- readable at
    a glance as "never ran", not confusable with UNKNOWN's own code."""
    result = subprocess.run(
        [sys.executable, str(MODULE_PATH)], capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert result.returncode != check.VERDICT_EXIT_CODES["UNKNOWN"]


def test_margin_bucket_does_not_block_overall_pass() -> None:
    """A row that hasn't crossed its kill line but also isn't at the ideal
    value (e.g. Memory pool end 4.5 GB: below the 5.0 'expected' but above
    the 4.0 kill line) must be reported distinctly as MARGINAL, and must
    NOT by itself flip the overall verdict away from PASS -- it only means
    "worth a human glance", not "confirmed unsafe"."""
    log = _synthetic_pass_log().replace("Memory pool end. avail mem=5.70 GB", "Memory pool end. avail mem=4.50 GB")
    rows, verdict = check.run_health_check(log)
    pool_row = next(r for r in rows if r.label.startswith("4 "))
    assert pool_row.status == "MARGINAL"
    assert verdict == "PASS"

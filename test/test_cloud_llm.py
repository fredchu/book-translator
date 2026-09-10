"""cloud_llm.sh 的守門與流程：用假 vastai／假 RunPod transport＋本機假 vLLM 伺服器跑真腳本，不租機、不花錢。

驗的是錢的分界：
- 沒金鑰 → 開機前就停，一張報價都不試
- 開機後伺服器一直沒就緒（預算 0 分鐘）→ 砍機退出，翻譯器不會被叫到
- 正常路徑（Vast）→ 等到 /v1/models 回應含模型名才叫翻譯器，翻譯器拿到 --omlx-host／--omlx-model／OMLX_API_KEY，翻完砍機並回查
- 正常路徑（RunPod）→ 走 v2 payload（args＋ports＋startSsh:false），端點從 runtime.ports 讀
- --keep → 不砍、寫 endpoint.env；--stop 才砍
"""

from __future__ import annotations

import http.server
import json
import os
import signal
import stat
import subprocess
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "cloud_llm.sh"
LIB_DIR = Path(os.environ.get("BOOK_TRANSLATOR_CLOUD_LIB_DIR", Path.home() / "dev/srt-skill/scripts"))
pytestmark = pytest.mark.skipif(not (LIB_DIR / "vast_instance_lib.sh").exists(), reason="需要 srt-skill 的共用檔")

MODEL = "XReyRobert/Qwopus3.6-27B-v2-GPTQ-Pro-v1"


class _Models(http.server.BaseHTTPRequestHandler):
    model = MODEL
    require_key: str | None = None
    probe_content: object = "連線測試。"
    probe_reasoning: object = None
    probe_requests: list[dict] = []

    def do_GET(self):  # noqa: N802
        if self.path != "/v1/models":
            self.send_response(404); self.end_headers(); return
        if self.require_key and self.headers.get("Authorization") != f"Bearer {self.require_key}":
            self.send_response(401); self.end_headers(); return
        body = json.dumps({"data": [{"id": self.model}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        if self.path != "/v1/chat/completions":
            self.send_response(404); self.end_headers(); return
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        self.__class__.probe_requests.append(request)
        body = json.dumps({
            "choices": [{
                "message": {
                    "content": self.__class__.probe_content,
                    "reasoning_content": self.__class__.probe_reasoning,
                },
                "finish_reason": "stop",
            }]
        }).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002  安靜
        pass


def _serve_models(
    require_key: str | None = None,
    *,
    probe_content: object = "連線測試。",
    probe_reasoning: object = None,
) -> tuple[http.server.HTTPServer, int]:
    handler = type("H", (_Models,), {
        "require_key": require_key,
        "probe_content": probe_content,
        "probe_reasoning": probe_reasoning,
        "probe_requests": [],
    })
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


FAKE_VASTAI = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >>"$FAKE_CALLS"
sub=(); i=0; args=("$@")
while [[ $i -lt ${#args[@]} ]]; do
    case "${args[$i]}" in --api-key) i=$((i+2)); continue ;; --raw) i=$((i+1)); continue ;; esac
    sub+=("${args[$i]}"); i=$((i+1))
done
case "${sub[0]} ${sub[1]}" in
    "search offers")    printf '%s\n' "$FAKE_OFFERS_JSON" ;;
    "create instance")  printf '%s\n' "$FAKE_CREATE_OUT" ;;
    "show instances")   if [[ -n "${FAKE_LIST_BY_LABEL:-}" ]] && ! grep -q "^destroyed" "$FAKE_CALLS"; then lbl=$(grep -o -- "--label [^ ]*" "$FAKE_CALLS" | head -1 | cut -d" " -f2); printf '[{"id":7001,"label":"%s","actual_status":"loading"}]\n' "$lbl"; exit 0; fi; printf '%s\n' "${FAKE_INSTANCES_JSON}" ;;
    "show instance")    printf '%s\n' "$FAKE_INSTANCE_JSON" ;;
    "destroy instance") echo destroyed >>"$FAKE_CALLS"; exit 0 ;;
    "logs "*) echo "fake container log" ;;
    *) exit 1 ;;
esac
"""

FAKE_TRANSLATE = r"""#!/usr/bin/env bash
printf 'ARGS: %s\nKEY: %s\n' "$*" "${OMLX_API_KEY:-}" >"$FAKE_TRANSLATE_OUT"
exit "${FAKE_TRANSLATE_RC:-0}"
"""

FAKE_ADAPTIVE_PROBE = r'''#!/usr/bin/env bash
out=""
while [[ $# -gt 0 ]]; do
    if [[ "$1" == --out ]]; then out="$2"; shift 2; else shift; fi
done
[[ "${FAKE_PROBE_FAIL:-0}" == 1 ]] && exit 7
safe="${FAKE_SAFE_CONCURRENCY:-16}"
passed="${FAKE_PROBE_PASSED:-true}"
printf '{"status":"ok","selected_concurrency":%s,"passed":%s,"waves":[{"n":%s,"mean_single_tok_s":%s,"max_latency_s":%s}]}\n' \
  "$safe" "$passed" "$safe" "${FAKE_PROBE_SPEED:-28.5}" "${FAKE_PROBE_LATENCY:-20}" >"$out"
'''

# 站在 `python3 -m bookcast.vast_machine_memory` 的位置——不管本機有沒有真的裝
# bookcast，測試都要能跑（跟這個檔案其餘部分同一個哲學：假外部依賴、跑真腳本）。
# FAKE_MACHINE_MEMORY_FAIL=1 專門模擬「bookcast 模組不在」這個 M4 要求的情境。
FAKE_MACHINE_MEMORY = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >>"$FAKE_MACHINE_MEMORY_CALLS"
if [[ "${FAKE_MACHINE_MEMORY_FAIL:-0}" == 1 ]]; then
    echo "ModuleNotFoundError: No module named 'bookcast'" >&2
    exit 1
fi
case "$1" in
    candidates) printf '%s\n' "${FAKE_MACHINE_MEMORY_CANDIDATES:-{\"preferred_ids\":[],\"bad_ids\":[]}}" ;;
    record)     exit 0 ;;
    *)          exit 1 ;;
esac
"""


def _setup(tmp_path: Path, *, port: int, offers=None) -> dict[str, str]:
    fake = tmp_path / "vastai"; fake.write_text(FAKE_VASTAI, encoding="utf-8"); fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    tr = tmp_path / "translate.sh"; tr.write_text(FAKE_TRANSLATE, encoding="utf-8"); tr.chmod(tr.stat().st_mode | stat.S_IXUSR)
    probe = tmp_path / "probe.sh"; probe.write_text(FAKE_ADAPTIVE_PROBE, encoding="utf-8"); probe.chmod(probe.stat().st_mode | stat.S_IXUSR)
    mm = tmp_path / "machine_memory.sh"; mm.write_text(FAKE_MACHINE_MEMORY, encoding="utf-8"); mm.chmod(mm.stat().st_mode | stat.S_IXUSR)
    calls = tmp_path / "calls.log"; calls.write_text("", encoding="utf-8")
    mm_calls = tmp_path / "mm_calls.log"; mm_calls.write_text("", encoding="utf-8")
    probe_load = tmp_path / "load.json"; probe_load.write_text('{"requests":[]}', encoding="utf-8")
    offers = offers if offers is not None else [{"id": 1, "gpu_name": "RTX 5090", "dph_total": 0.4, "geolocation": "KR", "public_ipaddr": "180.1.1.1"}]
    record = {"id": 7001, "actual_status": "running", "public_ipaddr": "127.0.0.1", "ports": {"8000/tcp": [{"HostPort": str(port)}]}, "status_msg": "ok", "machine_id": 555}
    env = dict(os.environ)
    env.update({
        "VAST_API_KEY": "mock", "VAST_LIB_CLI": str(fake), "FAKE_CALLS": str(calls),
        "BOOK_TRANSLATOR_CLOUD_LIB_DIR": str(LIB_DIR),
        "FAKE_OFFERS_JSON": json.dumps(offers), "FAKE_INSTANCE_JSON": json.dumps(record), "FAKE_INSTANCES_JSON": "[]",
        "CLOUD_LLM_TEST_TRANSLATE_CMD": str(tr), "FAKE_TRANSLATE_OUT": str(tmp_path / "translate.out"),
        "CLOUD_LLM_TEST_PROBE_CMD": str(probe), "CLOUD_LLM_PROBE_LOAD": str(probe_load),
        "CLOUD_LLM_POLL_SECONDS": "1", "CLOUD_LLM_BOOT_WAIT_MIN": "1",
        # 機器記憶一律走假指令——不依賴本機是否真的裝了 bookcast（測試要能在任何機器上跑）。
        "CLOUD_LLM_TEST_MACHINE_MEMORY_CMD": str(mm), "FAKE_MACHINE_MEMORY_CALLS": str(mm_calls),
        # args 模式 vastai 的真實回應格式（Python 字典字串，不是 JSON）；預設放這裡，不放假指令的 ${:-} 裡（大括號會被截斷）
        "FAKE_CREATE_OUT": "Started. {'success': True, 'new_contract': 7001, 'instance_api_key': 'x'}",
    })
    return env


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    # Use macOS's system Bash 3.2 rather than a Homebrew Bash found via PATH;
    # cloud_llm.sh must keep working with set -u when optional arrays are empty.
    return subprocess.run(["/bin/bash", str(SCRIPT), *args], capture_output=True, text=True, errors="replace", env=env, timeout=180, cwd=REPO)


def _calls(env) -> list[str]:
    return Path(env["FAKE_CALLS"]).read_text(encoding="utf-8").splitlines()


def _mm_calls(env) -> list[str]:
    return Path(env["FAKE_MACHINE_MEMORY_CALLS"]).read_text(encoding="utf-8").splitlines()


def test_no_key_stops_before_any_offer(tmp_path: Path) -> None:
    env = _setup(tmp_path, port=1); env.pop("VAST_API_KEY"); env["HOME"] = str(tmp_path / "nohome")
    r = _run(env, "--", "--book", "x.epub")
    assert r.returncode != 0 and "找不到 Vast.ai 金鑰" in r.stderr
    assert _calls(env) == []


def test_watchdog_hours_default_stays_six_for_single_book(tmp_path: Path) -> None:
    """現有行為不能變：一本書時看門狗仍是 6 小時。"""
    srv, port = _serve_models()
    try:
        env = _setup(tmp_path, port=port)
        r = _run(env, "--", "--book", "x.epub")
        assert r.returncode == 0, r.stderr[-1500:]
        assert "看門狗上限 6 小時（1 本書，依本數自動放大）" in r.stderr
    finally:
        srv.shutdown()


def test_watchdog_hours_scales_with_nargs_plus_book_count(tmp_path: Path) -> None:
    """--book 一次給多本（nargs="+"）：6 + 0.5×(本數-1)，3 本＝7 小時。"""
    srv, port = _serve_models()
    try:
        env = _setup(tmp_path, port=port)
        r = _run(env, "--", "--book", "a.epub", "b.epub", "c.epub")
        assert r.returncode == 0, r.stderr[-1500:]
        assert "看門狗上限 7 小時（3 本書，依本數自動放大）" in r.stderr
    finally:
        srv.shutdown()


def test_watchdog_hours_scales_with_repeated_book_flag(tmp_path: Path) -> None:
    """--book 重複給旗標（BookPathAction 疊加）一樣要算進去：2 本＝6.5 小時
    （半小時精度要顯示出來，不能被整數格式吃掉）。"""
    srv, port = _serve_models()
    try:
        env = _setup(tmp_path, port=port)
        r = _run(env, "--", "--book", "a.epub", "--book", "b.epub")
        assert r.returncode == 0, r.stderr[-1500:]
        assert "看門狗上限 6.5 小時（2 本書，依本數自動放大）" in r.stderr
    finally:
        srv.shutdown()


def test_watchdog_hours_counts_equals_form_mixed_with_nargs(tmp_path: Path) -> None:
    """--book=X 等號寫法混著 nargs="+" 一起也要算對：1（等號）+2（空格）＝3 本＝7 小時。"""
    srv, port = _serve_models()
    try:
        env = _setup(tmp_path, port=port)
        r = _run(env, "--", "--book=a.epub", "--book", "b.epub", "c.epub", "--out", "o")
        assert r.returncode == 0, r.stderr[-1500:]
        assert "看門狗上限 7 小時（3 本書，依本數自動放大）" in r.stderr
    finally:
        srv.shutdown()


def test_watchdog_hours_caps_at_twelve_and_dies_above_without_explicit(tmp_path: Path) -> None:
    """驗收：自動放大上限 12 小時。13 本剛好等於上限（放行），14 本超過就要求
    使用者明傳，不悄悄封頂在 12——書一多光靠額度往上加會在遠端燒到用戶不知道。"""
    srv, port = _serve_models()
    try:
        env = _setup(tmp_path, port=port)
        books_13 = [f"b{i}.epub" for i in range(13)]
        r = _run(env, "--", "--book", *books_13)
        assert r.returncode == 0, r.stderr[-1500:]
        assert "看門狗上限 12 小時（13 本書，依本數自動放大）" in r.stderr
    finally:
        srv.shutdown()

    over_dir = tmp_path / "over"; over_dir.mkdir()
    env2 = _setup(over_dir, port=1)
    books_14 = [f"b{i}.epub" for i in range(14)]
    r2 = _run(env2, "--", "--book", *books_14)
    assert r2.returncode != 0
    assert "超過自動放大上限 12 小時" in r2.stderr
    assert "請明傳 CLOUD_LLM_MAX_HOURS" in r2.stderr
    assert _calls(env2) == []  # 死在租機器之前，一張報價都沒試


def test_explicit_max_hours_accepts_fractional_value(tmp_path: Path) -> None:
    """使用者明傳也可以是小數（跟自動算式的半小時精度一致），驗證 awk 換算不會截斷。"""
    srv, port = _serve_models()
    try:
        env = _setup(tmp_path, port=port)
        env["CLOUD_LLM_MAX_HOURS"] = "6.5"
        r = _run(env, "--", "--book", "x.epub")
        assert r.returncode == 0, r.stderr[-1500:]
        assert "看門狗上限 6.5 小時（1 本書，CLOUD_LLM_MAX_HOURS 明傳）" in r.stderr
    finally:
        srv.shutdown()


def test_watchdog_hours_stops_counting_at_next_flag(tmp_path: Path) -> None:
    """--book a.epub --out dir：dir 不是書，不能被誤算成第二本。"""
    srv, port = _serve_models()
    try:
        env = _setup(tmp_path, port=port)
        r = _run(env, "--", "--book", "a.epub", "--out", "some-dir")
        assert r.returncode == 0, r.stderr[-1500:]
        assert "看門狗上限 6 小時（1 本書，依本數自動放大）" in r.stderr
    finally:
        srv.shutdown()


def test_explicit_max_hours_always_respected_even_with_many_books(tmp_path: Path) -> None:
    """使用者明傳 CLOUD_LLM_MAX_HOURS 一律尊重，不被本數放大蓋掉——即使明傳值比算式小。"""
    srv, port = _serve_models()
    try:
        env = _setup(tmp_path, port=port)
        env["CLOUD_LLM_MAX_HOURS"] = "2"
        r = _run(env, "--", "--book", "a.epub", "b.epub", "c.epub", "d.epub", "e.epub")
        assert r.returncode == 0, r.stderr[-1500:]
        assert "看門狗上限 2 小時（5 本書，CLOUD_LLM_MAX_HOURS 明傳）" in r.stderr
    finally:
        srv.shutdown()


def test_vast_happy_path_waits_for_models_then_translates_then_destroys(tmp_path: Path) -> None:
    srv, port = _serve_models()
    try:
        env = _setup(tmp_path, port=port)
        r = _run(env, "--provider", "vast", "--", "--book", "x.epub", "--out", "o")
        assert r.returncode == 0, r.stderr[-1500:]
        out = Path(env["FAKE_TRANSLATE_OUT"]).read_text(encoding="utf-8")
        assert f"--engine omlx --omlx-host http://127.0.0.1:{port} --omlx-model {MODEL}" in out
        assert "--concurrent-chapters --max-concurrent-requests 16" in out
        assert "--book x.epub --out o" in out
        assert len(srv.RequestHandlerClass.probe_requests) == 1
        assert srv.RequestHandlerClass.probe_requests[0]["chat_template_kwargs"] == {
            "enable_thinking": False
        }
        assert "vllm thinking preflight 通過" in r.stderr
        key = out.split("KEY: ")[1].strip(); assert len(key) >= 20
        calls = _calls(env)
        create = next(c for c in calls if "create instance" in c)
        assert "--image vllm/vllm-openai:v0.28.0" in create and "--env -p 8000:8000" in create
        assert f"--raw --args {MODEL} --served-model-name {MODEL} --port 8000" in create and f"--api-key {key}" in create
        assert "--max-num-seqs 24" in create
        assert "--ssh" not in create and "--disable-log-requests" not in create and not create.endswith("--raw")
        assert "destroyed" in calls and calls.index("destroyed") > calls.index(create)
        assert "已確認 7001 不在清單中" in r.stderr
    finally:
        srv.shutdown()


def test_cloud_concurrency_one_knob_and_escape_hatch(tmp_path: Path) -> None:
    srv, port = _serve_models()
    try:
        env = _setup(tmp_path, port=port)
        env["CLOUD_LLM_CONCURRENCY"] = "12"
        r = _run(env, "--", "--book", "x.epub")
        assert r.returncode == 0, r.stderr[-1500:]
        out = Path(env["FAKE_TRANSLATE_OUT"]).read_text(encoding="utf-8")
        assert "--concurrent-chapters --max-concurrent-requests 12" in out
        create = next(c for c in _calls(env) if "create instance" in c)
        assert "--max-num-seqs 24" in create

        off_dir = tmp_path / "off"
        off_dir.mkdir()
        env2 = _setup(off_dir, port=port)
        env2["CLOUD_LLM_CONCURRENT_CHAPTERS"] = "0"
        r2 = _run(env2, "--", "--book", "x.epub")
        assert r2.returncode == 0, r2.stderr[-1500:]
        out2 = Path(env2["FAKE_TRANSLATE_OUT"]).read_text(encoding="utf-8")
        assert "--concurrent-chapters" not in out2
        assert "--max-concurrent-requests" not in out2
    finally:
        srv.shutdown()


@pytest.mark.parametrize("safe", [24, 16, 12, 8])
def test_adaptive_probe_selects_each_candidate(tmp_path: Path, safe: int) -> None:
    srv, port = _serve_models()
    try:
        env = _setup(tmp_path, port=port)
        env["FAKE_SAFE_CONCURRENCY"] = str(safe)
        env["FAKE_PROBE_PASSED"] = "false" if safe == 8 else "true"
        r = _run(env, "--", "--book", "x.epub")
        assert r.returncode == 0, r.stderr
        out = Path(env["FAKE_TRANSLATE_OUT"]).read_text(encoding="utf-8")
        assert f"--max-concurrent-requests {safe}" in out
        assert f"安全 N={safe}" in r.stderr
        if safe == 8:
            assert "都未達 20% 跑飛餘裕" in r.stderr
    finally:
        srv.shutdown()


def test_adaptive_probe_failure_falls_back_without_stopping_translation(tmp_path: Path) -> None:
    srv, port = _serve_models()
    try:
        env = _setup(tmp_path, port=port)
        env["FAKE_PROBE_FAIL"] = "1"
        r = _run(env, "--", "--book", "x.epub")
        assert r.returncode == 0, r.stderr
        out = Path(env["FAKE_TRANSLATE_OUT"]).read_text(encoding="utf-8")
        assert "--max-concurrent-requests 16" in out
        assert "探針失敗" in r.stderr and "退回全域預設 16" in r.stderr
    finally:
        srv.shutdown()


def test_explicit_concurrency_still_probes_warns_and_is_respected(tmp_path: Path) -> None:
    srv, port = _serve_models()
    try:
        env = _setup(tmp_path, port=port)
        env["CLOUD_LLM_CONCURRENCY"] = "32"
        env["FAKE_SAFE_CONCURRENCY"] = "12"
        r = _run(env, "--", "--book", "x.epub")
        assert r.returncode == 0, r.stderr
        out = Path(env["FAKE_TRANSLATE_OUT"]).read_text(encoding="utf-8")
        assert "--max-concurrent-requests 32" in out
        create = next(c for c in _calls(env) if "create instance" in c)
        assert "--max-num-seqs 32" in create
        assert "明傳併發 32 高於探針安全值 12" in r.stderr
    finally:
        srv.shutdown()


def test_sglang_engine_uses_native_args_and_passes_thinking_preflight(tmp_path: Path) -> None:
    srv, port = _serve_models()
    try:
        env = _setup(tmp_path, port=port)
        env["CLOUD_LLM_ENGINE"] = "sglang"
        env["CLOUD_LLM_CONCURRENCY"] = "12"
        r = _run(env, "--", "--book", "x.epub")
        assert r.returncode == 0, r.stderr[-2000:]
        create = next(c for c in _calls(env) if "create instance" in c)
        assert "--image lmsysorg/sglang:latest-runtime" in create
        assert f"--raw --args python3 -m sglang.launch_server --model-path {MODEL}" in create
        assert "--context-length 16384" in create
        assert "--mem-fraction-static 0.92" in create
        assert "--max-running-requests 24" in create
        assert "--reasoning-parser qwen3" in create
        assert "--max-model-len" not in create
        assert "--gpu-memory-utilization" not in create
        assert "--max-num-seqs" not in create
        requests = srv.RequestHandlerClass.probe_requests
        assert len(requests) == 1
        assert requests[0]["chat_template_kwargs"] == {"enable_thinking": False}
        assert Path(env["FAKE_TRANSLATE_OUT"]).exists()
        assert "thinking preflight 通過" in r.stderr
        assert "destroyed" in _calls(env)
    finally:
        srv.shutdown()


@pytest.mark.parametrize(
    ("engine", "extra"),
    [("vllm", "--max-num-seqs=8"), ("sglang", "--max-running-requests 8")],
)
def test_rejects_server_ceiling_override_before_renting(
    tmp_path: Path, engine: str, extra: str
) -> None:
    env = _setup(tmp_path, port=1)
    env["CLOUD_LLM_ENGINE"] = engine
    env["CLOUD_LLM_EXTRA_SERVER_ARGS"] = extra
    r = _run(env, "--", "--book", "x.epub")
    assert r.returncode != 0
    assert "server 併發上限由自適應探針守衛" in r.stderr
    assert _calls(env) == []


def test_sglang_rejects_reasoning_parser_override_before_renting(tmp_path: Path) -> None:
    env = _setup(tmp_path, port=1)
    env["CLOUD_LLM_ENGINE"] = "sglang"
    env["CLOUD_LLM_EXTRA_SERVER_ARGS"] = "--reasoning-parser deepseek-r1"
    r = _run(env, "--", "--book", "x.epub")
    assert r.returncode != 0
    assert "reasoning-parser qwen3 是強制品質守衛" in r.stderr
    assert _calls(env) == []
    assert not Path(env["FAKE_TRANSLATE_OUT"]).exists()


@pytest.mark.parametrize("engine", ["vllm", "sglang"])
@pytest.mark.parametrize(
    ("probe_content", "probe_reasoning", "expected"),
    [
        ("<think>secret</think>譯文", None, "content 含 think 標籤"),
        ("<think\n>secret</think>譯文", None, "content 含 think 標籤"),
        ("譯文", "secret", "reasoning_content 非空"),
        ("譯文", False, "reasoning_content 非空"),
        ("譯文", {"text": "secret"}, "reasoning_content 非空"),
        ("", None, "content 必須是非空字串"),
        ({"text": "譯文"}, None, "content 必須是非空字串"),
        (None, None, "content 必須是非空字串"),
    ],
)
def test_thinking_preflight_fails_closed_and_destroys(
    tmp_path: Path, engine: str, probe_content: object,
    probe_reasoning: object, expected: str
) -> None:
    srv, port = _serve_models(
        probe_content=probe_content, probe_reasoning=probe_reasoning
    )
    try:
        env = _setup(tmp_path, port=port)
        env["CLOUD_LLM_ENGINE"] = engine
        r = _run(env, "--", "--book", "x.epub")
        assert r.returncode != 0
        assert expected in r.stderr
        assert not Path(env["FAKE_TRANSLATE_OUT"]).exists()
        assert "destroyed" in _calls(env)
    finally:
        srv.shutdown()


def test_server_never_ready_terminates_without_calling_translator(tmp_path: Path) -> None:
    srv, port = _serve_models()
    srv.shutdown()  # 埠已關：端點在、/v1/models 永遠打不到
    env = _setup(tmp_path, port=port); env["CLOUD_LLM_BOOT_WAIT_MIN"] = "0"
    r = _run(env, "--", "--book", "x.epub")
    assert r.returncode != 0 and "還沒就緒" in r.stderr
    assert not Path(env["FAKE_TRANSLATE_OUT"]).exists()
    assert "destroyed" in _calls(env)


def test_wrong_api_key_is_not_ready(tmp_path: Path) -> None:
    # 伺服器要求金鑰 K，但腳本每次產生新的隨機金鑰 → 永遠 401 → 不算就緒 → 砍機。驗證就緒判定真的看 200+模型名。
    srv, port = _serve_models(require_key="K")
    try:
        env = _setup(tmp_path, port=port); env["CLOUD_LLM_BOOT_WAIT_MIN"] = "0"
        r = _run(env, "--", "--book", "x.epub")
        assert r.returncode != 0 and "destroyed" in _calls(env)
    finally:
        srv.shutdown()


def test_keep_then_stop(tmp_path: Path) -> None:
    srv, port = _serve_models()
    try:
        env = _setup(tmp_path, port=port)
        r = _run(env, "--keep", "--", "--book", "x.epub")
        assert r.returncode == 0, r.stderr[-1000:]
        assert "destroyed" not in _calls(env)
        run_dir = Path(r.stderr.split("砍：")[-1].split("--stop ")[-1].split()[0])
        assert (run_dir / "instance.id").read_text().strip() == "7001"
        ep = (run_dir / "endpoint.env").read_text()
        assert f"OMLX_HOST=http://127.0.0.1:{port}" in ep and "OMLX_API_KEY=" in ep
        r2 = _run(env, "--stop", str(run_dir))
        assert r2.returncode == 0 and "destroyed" in _calls(env)
        assert not (run_dir / "instance.id").exists()
    finally:
        srv.shutdown()


def test_runpod_path_builds_v2_payload_and_reads_runtime_ports(tmp_path: Path) -> None:
    srv, port = _serve_models()
    try:
        env = _setup(tmp_path, port=port)
        env.pop("VAST_API_KEY"); env["RUNPOD_API_KEY"] = "mock"
        hook = tmp_path / "hook.sh"
        hook.write_text(f"""
cloud_llm_test_after_libs() {{
    fake_rp() {{
        local method="$1" path="$2" body="$3"
        printf 'RP %s %s %s\\n' "$method" "$path" "$(cat "$body" 2>/dev/null | tr -d '\\n')" >>"$FAKE_CALLS"
        case "$method $path" in
            "POST /pods") printf '{{"id":"pod9"}}\\n' ;;
            "GET /pods/pod9") printf '{{"id":"pod9","status":"RUNNING","runtime":{{"ports":[{{"private":8000,"public":{port},"type":"tcp","ip":"127.0.0.1"}}]}}}}\\n' ;;
            "GET /pods") printf '[]\\n' ;;
            "DELETE /pods/pod9") echo destroyed >>"$FAKE_CALLS" ;;
            *) return 1 ;;
        esac
    }}
    RUNPOD_LIB_TRANSPORT=fake_rp
}}
""", encoding="utf-8")
        env["CLOUD_LLM_TEST_HOOK_FILE"] = str(hook)
        r = _run(env, "--provider", "runpod", "--", "--book", "x.epub")
        assert r.returncode == 0, r.stderr[-1500:]
        calls = _calls(env)
        post = next(c for c in calls if c.startswith("RP POST /pods"))
        payload = json.loads(post.split(" ", 3)[3])
        assert payload["image"] == "vllm/vllm-openai:v0.28.0" and payload["ports"] == ["8000/tcp"] and payload["startSsh"] is False
        assert payload["gpu"]["id"] == "NVIDIA GeForce RTX 5090" and payload["args"].startswith(f"{MODEL} --served-model-name")
        assert "destroyed" in calls
        out = Path(env["FAKE_TRANSLATE_OUT"]).read_text(encoding="utf-8")
        assert f"--omlx-host http://127.0.0.1:{port}" in out
    finally:
        srv.shutdown()


def test_fp8_profile_switches_model_and_gpu(tmp_path: Path) -> None:
    srv, port = _serve_models()
    srv.shutdown()
    env = _setup(tmp_path, port=port); env["CLOUD_LLM_BOOT_WAIT_MIN"] = "0"
    _run(env, "--profile", "fp8", "--", "--book", "x.epub")
    q = next(c for c in _calls(env) if "search offers" in c)
    assert "gpu_name=L40S" in q and "dph_total<=1.2" in q
    create = next(c for c in _calls(env) if "create instance" in c)
    assert "--args Jackrong/Qwopus3.6-27B-v2-FP8 " in create


def test_machine_memory_records_created_and_synth_ok(tmp_path: Path) -> None:
    """M3: 成功各記一筆，stage 標明是 book-translator（spec-03）。"""
    srv, port = _serve_models()
    try:
        env = _setup(tmp_path, port=port)
        r = _run(env, "--", "--book", "x.epub")
        assert r.returncode == 0, r.stderr[-1500:]
        mm_calls = _mm_calls(env)
        created = [c for c in mm_calls if c.startswith("record") and "--event created" in c]
        ok = [c for c in mm_calls if c.startswith("record") and "--event synth_ok" in c]
        assert len(created) == 1, mm_calls
        assert len(ok) == 1, mm_calls
        for call in (created[0], ok[0]):
            assert "--machine-id 555" in call
            assert "--stage book-translator" in call
    finally:
        srv.shutdown()


def test_machine_memory_records_boot_failed_on_stall_with_reason(tmp_path: Path) -> None:
    """M3: 停滯偵測放棄時 record 一筆失敗，reason=image_loading_stall（spec-03，硬性要求）。"""
    env = _setup(tmp_path, port=1)
    env["CLOUD_LLM_BOOT_STALL_MIN"] = "0"
    record = {
        "id": 7001, "actual_status": "loading", "public_ipaddr": "127.0.0.1",
        "ports": {}, "status_msg": "pulling image layer 3/10", "machine_id": 555,
    }
    env["FAKE_INSTANCE_JSON"] = json.dumps(record)
    r = _run(env, "--", "--book", "x.epub")
    assert r.returncode != 0
    assert "拉映像進度" in r.stderr and "已砍機" in r.stderr
    failed = [c for c in _mm_calls(env) if c.startswith("record") and "--event boot_failed" in c]
    assert len(failed) == 1, _mm_calls(env)
    assert "--reason image_loading_stall" in failed[0]
    assert "--machine-id 555" in failed[0]
    assert "destroyed" in _calls(env)


def test_machine_memory_records_boot_failed_on_dead_status(tmp_path: Path) -> None:
    env = _setup(tmp_path, port=1)
    record = {
        "id": 7001, "actual_status": "exited", "public_ipaddr": "127.0.0.1",
        "ports": {}, "status_msg": "", "machine_id": 555,
    }
    env["FAKE_INSTANCE_JSON"] = json.dumps(record)
    r = _run(env, "--", "--book", "x.epub")
    assert r.returncode != 0
    failed = [c for c in _mm_calls(env) if c.startswith("record") and "--event boot_failed" in c]
    assert len(failed) == 1, _mm_calls(env)
    assert "--reason dead_status" in failed[0]


def test_machine_memory_not_recorded_on_user_interrupt(tmp_path: Path) -> None:
    """按 Ctrl-C／收到 SIGTERM 不算機器壞掉——只有腳本自己 die() 觸發的 EXIT 路徑
    才記失敗（照抄 bookcast on_exit 的判斷：INT/TERM/HUP 各自的 trap 不記）。"""
    env = _setup(tmp_path, port=1)
    env["CLOUD_LLM_BOOT_WAIT_MIN"] = "5"  # 給足時間，確保訊號送到時還在等待就緒迴圈
    proc = subprocess.Popen(
        ["/bin/bash", str(SCRIPT), "--", "--book", "x.epub"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=REPO,
    )
    try:
        for _ in range(300):
            line = proc.stderr.readline()
            if not line:
                pytest.fail("腳本提早結束，來不及送 SIGTERM")
            if "已開機" in line:
                break
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill(); proc.communicate()
    assert proc.returncode == 143
    assert "destroyed" in _calls(env)
    failed = [c for c in _mm_calls(env) if c.startswith("record") and "--event boot_failed" in c]
    assert failed == [], _mm_calls(env)


def test_bookcast_module_missing_fails_soft_and_still_rents(tmp_path: Path) -> None:
    """M4：bookcast 模組不在（或資料庫壞掉）一律印警告、照常租機器——不可以 die。"""
    srv, port = _serve_models()
    try:
        env = _setup(tmp_path, port=port)
        env["FAKE_MACHINE_MEMORY_FAIL"] = "1"
        r = _run(env, "--", "--book", "x.epub")
        assert r.returncode == 0, r.stderr[-1500:]
        assert "機器記憶" in r.stderr
        assert "退回一般搜尋" in r.stderr
        out = Path(env["FAKE_TRANSLATE_OUT"]).read_text(encoding="utf-8")
        assert out  # 翻譯器真的被叫到了——租機器沒有被機器記憶失敗擋下來
        assert "destroyed" in _calls(env)
    finally:
        srv.shutdown()


def test_preferred_ids_are_injected_into_offer_query(tmp_path: Path) -> None:
    """驗收 1：candidates 取得的 preferred/blocked 有被實際用在挑機（不是取了不用）。"""
    srv, port = _serve_models()
    try:
        env = _setup(tmp_path, port=port)
        env["FAKE_MACHINE_MEMORY_CANDIDATES"] = json.dumps({"preferred_ids": [42, 43], "bad_ids": []})
        r = _run(env, "--", "--book", "x.epub")
        assert r.returncode == 0, r.stderr[-1500:]
        search = next(c for c in _calls(env) if "search offers" in c)
        assert "machine_id in [42,43]" in search
        assert "白名單" in r.stderr
    finally:
        srv.shutdown()


def test_bad_ids_are_excluded_from_general_offer_query(tmp_path: Path) -> None:
    srv, port = _serve_models()
    try:
        env = _setup(tmp_path, port=port)
        env["FAKE_MACHINE_MEMORY_CANDIDATES"] = json.dumps({"preferred_ids": [], "bad_ids": [99]})
        r = _run(env, "--", "--book", "x.epub")
        assert r.returncode == 0, r.stderr[-1500:]
        search = next(c for c in _calls(env) if "search offers" in c)
        assert "machine_id notin [99]" in search
    finally:
        srv.shutdown()


def test_empty_preferred_list_never_sends_empty_machine_id_in_clause(tmp_path: Path) -> None:
    srv, port = _serve_models()
    try:
        env = _setup(tmp_path, port=port)  # 預設空白名單、空黑名單
        r = _run(env, "--", "--book", "x.epub")
        assert r.returncode == 0, r.stderr[-1500:]
        search = next(c for c in _calls(env) if "search offers" in c)
        assert "machine_id in [" not in search
        assert "machine_id notin [" not in search
    finally:
        srv.shutdown()


def test_whitelist_success_skips_general_search_entirely(tmp_path: Path) -> None:
    srv, port = _serve_models()
    try:
        env = _setup(tmp_path, port=port)
        env["FAKE_MACHINE_MEMORY_CANDIDATES"] = json.dumps({"preferred_ids": [42], "bad_ids": []})
        r = _run(env, "--", "--book", "x.epub")
        assert r.returncode == 0, r.stderr[-1500:]
        searches = [c for c in _calls(env) if "search offers" in c]
        assert len(searches) == 1  # 白名單第一次就成功，一般搜尋完全沒被呼叫
        creates = [c for c in _calls(env) if "create instance" in c]
        assert len(creates) == 1
    finally:
        srv.shutdown()


def test_tier_budgets_are_independent_not_shared(tmp_path: Path) -> None:
    """驗收 2（硬性要求，09-09 教訓）：三層挑機額度各自獨立。白名單用完自己的
    WHITELIST_TRIES（預設 2）張都失敗，一般搜尋仍拿到完整的 MAX_OFFER_TRIES
    （預設 3）張可試——不是共用一個縮水後的額度。"""
    offers = [
        {"id": i, "gpu_name": "RTX 5090", "dph_total": 0.4, "geolocation": "KR", "public_ipaddr": f"180.1.1.{i}"}
        for i in range(1, 8)
    ]
    env = _setup(tmp_path, port=1, offers=offers)  # port 用不到——create 全部失敗，永遠到不了就緒檢查
    env["FAKE_CREATE_OUT"] = "garbage"  # 每次 create 都解析失敗，且不設 FAKE_LIST_BY_LABEL（不會被認領）
    env["FAKE_MACHINE_MEMORY_CANDIDATES"] = json.dumps({"preferred_ids": [42], "bad_ids": []})
    r = _run(env, "--", "--book", "x.epub")
    assert r.returncode != 0
    assert "Vast.ai 開機失敗" in r.stderr
    creates = [c for c in _calls(env) if "create instance" in c]
    # 2（白名單）+ 3（一般，bad_ids 空所以沒有第三層）＝ 5，不是任何比 5 小的數字
    assert len(creates) == 5, (len(creates), r.stderr[-800:])
    assert "白名單試報價" in r.stderr
    assert "一般試報價" in r.stderr
    assert "緊急試報價" not in r.stderr  # bad_ids 空，第三層不應該被觸發


def test_emergency_tier_gets_own_full_budget_when_general_exhausted(tmp_path: Path) -> None:
    offers = [
        {"id": i, "gpu_name": "RTX 5090", "dph_total": 0.4, "geolocation": "KR", "public_ipaddr": f"180.1.1.{i}"}
        for i in range(1, 8)
    ]
    env = _setup(tmp_path, port=1, offers=offers)
    env["FAKE_CREATE_OUT"] = "garbage"
    env["FAKE_MACHINE_MEMORY_CANDIDATES"] = json.dumps({"preferred_ids": [], "bad_ids": [99]})
    r = _run(env, "--", "--book", "x.epub")
    assert r.returncode != 0
    assert "Vast.ai 開機失敗" in r.stderr
    creates = [c for c in _calls(env) if "create instance" in c]
    # 沒有白名單；一般（3）＋緊急（3，各自完整額度）＝ 6
    assert len(creates) == 6, (len(creates), r.stderr[-800:])
    assert "一般試報價" in r.stderr
    assert "緊急試報價" in r.stderr


def test_unparseable_create_response_adopts_instance_by_label_instead_of_retrying(tmp_path: Path) -> None:
    # 回應完全看不懂，但清單裡已有本次 label 的機器 → 認領，不再開第二台（09-04 連漏三台的修法）
    srv, port = _serve_models()
    try:
        env = _setup(tmp_path, port=port)
        env["FAKE_CREATE_OUT"] = "garbage"
        env["FAKE_LIST_BY_LABEL"] = "1"
        r = _run(env, "--", "--book", "x.epub")
        assert r.returncode == 0, r.stderr[-1500:]
        calls = _calls(env)
        assert sum(1 for c in calls if "create instance" in c) == 1, calls
        assert "認領它" in r.stderr and "destroyed" in calls
    finally:
        srv.shutdown()

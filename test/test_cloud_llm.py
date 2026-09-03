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

    def do_GET(self):  # noqa: N802
        if self.path != "/v1/models":
            self.send_response(404); self.end_headers(); return
        if self.require_key and self.headers.get("Authorization") != f"Bearer {self.require_key}":
            self.send_response(401); self.end_headers(); return
        body = json.dumps({"data": [{"id": self.model}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002  安靜
        pass


def _serve_models(require_key: str | None = None) -> tuple[http.server.HTTPServer, int]:
    handler = type("H", (_Models,), {"require_key": require_key})
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


def _setup(tmp_path: Path, *, port: int, offers=None) -> dict[str, str]:
    fake = tmp_path / "vastai"; fake.write_text(FAKE_VASTAI, encoding="utf-8"); fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    tr = tmp_path / "translate.sh"; tr.write_text(FAKE_TRANSLATE, encoding="utf-8"); tr.chmod(tr.stat().st_mode | stat.S_IXUSR)
    calls = tmp_path / "calls.log"; calls.write_text("", encoding="utf-8")
    offers = offers if offers is not None else [{"id": 1, "gpu_name": "RTX 5090", "dph_total": 0.4, "geolocation": "KR", "public_ipaddr": "180.1.1.1"}]
    record = {"id": 7001, "actual_status": "running", "public_ipaddr": "127.0.0.1", "ports": {"8000/tcp": [{"HostPort": str(port)}]}, "status_msg": "ok"}
    env = dict(os.environ)
    env.update({
        "VAST_API_KEY": "mock", "VAST_LIB_CLI": str(fake), "FAKE_CALLS": str(calls),
        "BOOK_TRANSLATOR_CLOUD_LIB_DIR": str(LIB_DIR),
        "FAKE_OFFERS_JSON": json.dumps(offers), "FAKE_INSTANCE_JSON": json.dumps(record), "FAKE_INSTANCES_JSON": "[]",
        "CLOUD_LLM_TEST_TRANSLATE_CMD": str(tr), "FAKE_TRANSLATE_OUT": str(tmp_path / "translate.out"),
        "CLOUD_LLM_POLL_SECONDS": "1", "CLOUD_LLM_BOOT_WAIT_MIN": "1",
        # args 模式 vastai 的真實回應格式（Python 字典字串，不是 JSON）；預設放這裡，不放假指令的 ${:-} 裡（大括號會被截斷）
        "FAKE_CREATE_OUT": "Started. {'success': True, 'new_contract': 7001, 'instance_api_key': 'x'}",
    })
    return env


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, errors="replace", env=env, timeout=180, cwd=REPO)


def _calls(env) -> list[str]:
    return Path(env["FAKE_CALLS"]).read_text(encoding="utf-8").splitlines()


def test_no_key_stops_before_any_offer(tmp_path: Path) -> None:
    env = _setup(tmp_path, port=1); env.pop("VAST_API_KEY"); env["HOME"] = str(tmp_path / "nohome")
    r = _run(env, "--", "--book", "x.epub")
    assert r.returncode != 0 and "找不到 Vast.ai 金鑰" in r.stderr
    assert _calls(env) == []


def test_vast_happy_path_waits_for_models_then_translates_then_destroys(tmp_path: Path) -> None:
    srv, port = _serve_models()
    try:
        env = _setup(tmp_path, port=port)
        r = _run(env, "--provider", "vast", "--", "--book", "x.epub", "--out", "o")
        assert r.returncode == 0, r.stderr[-1500:]
        out = Path(env["FAKE_TRANSLATE_OUT"]).read_text(encoding="utf-8")
        assert f"--engine omlx --omlx-host http://127.0.0.1:{port} --omlx-model {MODEL} --book x.epub --out o" in out
        key = out.split("KEY: ")[1].strip(); assert len(key) >= 20
        calls = _calls(env)
        create = next(c for c in calls if "create instance" in c)
        assert "--image vllm/vllm-openai:v0.28.0" in create and "--env -p 8000:8000" in create
        assert f"--raw --args {MODEL} --served-model-name {MODEL} --port 8000" in create and f"--api-key {key}" in create
        assert "--ssh" not in create and "--disable-log-requests" not in create and not create.endswith("--raw")
        assert "destroyed" in calls and calls.index("destroyed") > calls.index(create)
        assert "已確認 7001 不在清單中" in r.stderr
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

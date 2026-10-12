"""cloud_llm.sh 走 Colab 的守門與流程：假 colab、假 ssh＋本機假 vLLM 伺服器跑真腳本，不租機、不花錢。

錢的分界：
- 只支援 vllm、不支援 --keep → 開機前就擋
- 登入失效 → 開機前就停，不呼叫 colab new
- 開機腳本結束（vLLM 沒起來）→ 砍機、翻譯器不會被叫到
- 正常路徑 → 端點是本機轉送埠；翻完 colab stop 並回查清單確認消失；開機後透過 kernel 戳機器
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_cloud_llm import FAKE_ADAPTIVE_PROBE, FAKE_TRANSLATE, MODEL, _serve_models  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "cloud_llm.sh"
pytestmark = pytest.mark.skipif(os.name == "nt", reason="Windows 沒有 bash")

FAKE_COLAB = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >>"$FAKE_CALLS"
case "$1" in
    usage)
        [[ "${FAKE_AUTH_FAIL:-0}" == 1 ]] && { echo "Enter the authorization code: EOF"; exit 0; }
        echo "Current balance: 200.00 compute units"; echo "Active assignments: 0" ;;
    sessions)
        while IFS= read -r s; do [[ -n "$s" ]] && echo "[$s] gpu | Hardware: A100"; done <"$FAKE_SESSIONS" ;;
    new) echo "$3" >>"$FAKE_SESSIONS"; echo "[colab] Session READY." ;;
    stop) grep -vxF "$3" "$FAKE_SESSIONS" >"$FAKE_SESSIONS.tmp" || true; mv "$FAKE_SESSIONS.tmp" "$FAKE_SESSIONS" ;;
    exec) cat >/dev/null ;;
    *) exit 1 ;;
esac
"""

# 假 ssh：常駐連線用標記檔模擬（-fN 建立、-O check 查、-O exit 關）；遠端指令依內容回應
FAKE_SSH = r"""#!/usr/bin/env bash
printf 'ssh %s\n' "$*" >>"$FAKE_CALLS"
case " $* " in
    *" -O check "*) [[ -f "$FAKE_CALLS.master" ]]; exit $? ;;
    *" -fN "*) touch "$FAKE_CALLS.master"; exit 0 ;;
    *" -O exit "*) rm -f "$FAKE_CALLS.master"; exit 0 ;;
esac
last="${@: -1}"
case "$last" in
    *"cat > /workspace/boot.sh"*) cat >"$FAKE_BOOT_SH" ;;
    *boot.rc*SERVER_START*) echo "${FAKE_REMOTE_STATUS:-serving}" ;;
    *"tail -20 /workspace/boot.log"*) echo "Traceback: fake vllm crash" ;;
    *) : ;;
esac
exit 0
"""

FAKE_TIMEOUT = r"""#!/usr/bin/env bash
[[ "$1" == "-k" ]] && shift 2
shift
exec "$@"
"""


def _setup(tmp_path: Path, port: int) -> dict[str, str]:
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    for name, body in (("colab", FAKE_COLAB), ("ssh", FAKE_SSH), ("timeout", FAKE_TIMEOUT),
                       ("translate.sh", FAKE_TRANSLATE), ("probe.sh", FAKE_ADAPTIVE_PROBE)):
        f = bin_dir / name; f.write_text(body, encoding="utf-8"); f.chmod(f.stat().st_mode | stat.S_IXUSR)
    home = tmp_path / "home"; (home / ".config/colab-cli").mkdir(parents=True)
    (home / ".config/colab-cli/settings.json").write_text('{"enable_update_check": true}', encoding="utf-8")
    (home / ".ssh").mkdir()
    (home / ".ssh/id_ed25519").write_text("k", encoding="utf-8")
    (home / ".ssh/id_ed25519.pub").write_text("ssh-ed25519 AAAA t", encoding="utf-8")
    (tmp_path / "calls.log").write_text("", encoding="utf-8")
    (tmp_path / "sessions").write_text("", encoding="utf-8")
    load = tmp_path / "load.json"; load.write_text('{"requests":[]}', encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "HOME": str(home), "PATH": f"{bin_dir}:{env['PATH']}",
        "FAKE_CALLS": str(tmp_path / "calls.log"), "FAKE_SESSIONS": str(tmp_path / "sessions"),
        "FAKE_BOOT_SH": str(tmp_path / "boot.sh"),
        "CLOUD_LLM_PROVIDER": "colab", "CLOUD_LLM_COLAB_BIN": str(bin_dir / "colab"),
        "CLOUD_LLM_COLAB_LOCAL_PORT": str(port), "CLOUD_LLM_COLAB_KEEPALIVE_SEC": "1",
        "CLOUD_LLM_TEST_TRANSLATE_CMD": str(bin_dir / "translate.sh"), "FAKE_TRANSLATE_OUT": str(tmp_path / "translate.out"),
        "CLOUD_LLM_TEST_PROBE_CMD": str(bin_dir / "probe.sh"), "CLOUD_LLM_PROBE_LOAD": str(load),
        "CLOUD_LLM_POLL_SECONDS": "1", "CLOUD_LLM_BOOT_WAIT_MIN": "1",
    })
    return env


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["/bin/bash", str(SCRIPT), *args], capture_output=True, text=True, errors="replace",
                          env=env, timeout=180, cwd=REPO)


def _calls(env) -> list[str]:
    return Path(env["FAKE_CALLS"]).read_text(encoding="utf-8").splitlines()


def test_happy_path_tunnels_endpoint_translates_and_stops(tmp_path: Path) -> None:
    srv, port = _serve_models()
    try:
        env = _setup(tmp_path, port)
        r = _run(env, "--", "--book", "x.epub")
    finally:
        srv.shutdown()
    assert r.returncode == 0, r.stderr
    calls = _calls(env)
    new = [c for c in calls if c.startswith("new ")]
    assert len(new) == 1 and "--gpu A100" in new[0]
    label = new[0].split()[2]
    assert f"stop -s {label}" in calls
    assert Path(env["FAKE_SESSIONS"]).read_text(encoding="utf-8").strip() == ""
    assert any(c.startswith("exec -s ") for c in calls), "開機後沒有透過 kernel 戳機器"
    assert any("-o ControlMaster=yes" in c and f"-L 127.0.0.1:{port}:127.0.0.1:8000" in c for c in calls)
    boot = Path(env["FAKE_BOOT_SH"]).read_text(encoding="utf-8")
    assert "vllm==0.28.0" in boot and "vllm serve" in boot and "--api-key" in boot
    assert "export PATH=/workspace/vllm-venv/bin:$PATH" in boot, "ninja 要在 PATH 上"
    out = Path(env["FAKE_TRANSLATE_OUT"]).read_text(encoding="utf-8")
    assert f"--omlx-host http://127.0.0.1:{port}" in out
    assert "--concurrent-chapters --max-concurrent-requests 16" in out
    assert '"enable_update_check": false' in (Path(env["HOME"]) / ".config/colab-cli/settings.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("extra_env, args, msg", [
    ({"CLOUD_LLM_ENGINE": "sglang"}, [], "只支援 CLOUD_LLM_ENGINE=vllm"),
    ({}, ["--keep"], "不支援 --keep"),
    ({"FAKE_AUTH_FAIL": "1"}, [], "Colab 登入失效"),
])
def test_guards_stop_before_renting(tmp_path: Path, extra_env, args, msg) -> None:
    env = _setup(tmp_path, 1); env.update(extra_env)
    r = _run(env, *args, "--", "--book", "x.epub")
    assert r.returncode != 0 and msg in r.stderr
    assert not any(c.startswith("new ") for c in _calls(env))


def test_server_boot_failure_stops_without_translating(tmp_path: Path) -> None:
    env = _setup(tmp_path, 1); env["FAKE_REMOTE_STATUS"] = "exited:1"
    r = _run(env, "--", "--book", "x.epub")
    assert r.returncode != 0 and "vLLM 開機腳本結束了" in r.stderr
    assert any(c.startswith("stop -s ") for c in _calls(env))
    assert not Path(env["FAKE_TRANSLATE_OUT"]).exists()
    assert Path(env["FAKE_SESSIONS"]).read_text(encoding="utf-8").strip() == ""

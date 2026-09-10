"""Self-contained cloud engine contract tests.

Unlike test_cloud_llm.py these do not require the sibling srt-skill checkout,
so the vLLM/SGLang thinking guard runs in repository-only CI.
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
MODEL = "XReyRobert/Qwopus3.6-27B-v2-GPTQ-Pro-v1"


class _Server(http.server.BaseHTTPRequestHandler):
    content: object = "連線測試。"
    reasoning: object = None
    requests: list[dict] = []

    def do_GET(self):  # noqa: N802
        body = json.dumps({"data": [{"id": MODEL}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        size = int(self.headers.get("Content-Length", "0"))
        self.__class__.requests.append(json.loads(self.rfile.read(size)))
        body = json.dumps({
            "choices": [{"message": {
                "content": self.__class__.content,
                "reasoning_content": self.__class__.reasoning,
            }}]
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        pass


def _serve(*, content: object = "連線測試。", reasoning: object = None):
    handler = type("Handler", (_Server,), {
        "content": content, "reasoning": reasoning, "requests": []
    })
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


VAST_STUB = r'''#!/usr/bin/env bash
VAST_LIB_CLI=true
VAST_LIB_TERMINATE_LAST_ERROR=""
vast_lib_load_api_key() { return 0; }
vast_lib_pick_offers() { printf '1\tfake offer\n'; }
vast_lib_create_instance_args() {
    printf 'create %s\n' "$*" >>"$STUB_CALLS"
    printf '7001\n'
}
vast_lib_find_live_instance_by_label() { return 1; }
vast_lib_instance_record() {
    printf '{"id":7001,"actual_status":"running","status_msg":"ok"}\n'
}
vast_lib_status_is_dead() { return 1; }
vast_lib_port_endpoint_from_record() { printf '127.0.0.1\t%s\n' "$STUB_PORT"; }
vast_lib_terminate_instance_once() { echo destroyed >>"$STUB_CALLS"; return 0; }
vast_lib_cli() { return 0; }
'''

TRANSLATE_STUB = r'''#!/usr/bin/env bash
printf '%s\n' "$*" >"$TRANSLATE_OUT"
'''

PROBE_STUB = r'''#!/usr/bin/env bash
out=""; gpu=""; profile=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --out) out="$2"; shift 2 ;;
        --gpu) gpu="$2"; shift 2 ;;
        --profile) profile="$2"; shift 2 ;;
        *) shift ;;
    esac
done
[[ "${FAKE_PROBE_FAIL:-0}" == 1 ]] && exit 7
safe="${FAKE_SAFE_CONCURRENCY:-16}"; passed="${FAKE_PROBE_PASSED:-true}"
printf '{"status":"ok","gpu":"%s","profile":"%s","selected_concurrency":%s,"passed":%s,"waves":[{"n":%s,"mean_single_tok_s":%s,"max_latency_s":%s}]}\n' \
  "$gpu" "$profile" "$safe" "$passed" "$safe" "${FAKE_PROBE_SPEED:-28.5}" "${FAKE_PROBE_LATENCY:-20}" >"$out"
[[ -n "${PROBE_CAPTURE:-}" ]] && cp "$out" "$PROBE_CAPTURE"
'''


def _env(tmp_path: Path, port: int, engine: str) -> dict[str, str]:
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "vast_instance_lib.sh").write_text(VAST_STUB, encoding="utf-8")
    translate = tmp_path / "translate.sh"
    translate.write_text(TRANSLATE_STUB, encoding="utf-8")
    translate.chmod(translate.stat().st_mode | stat.S_IXUSR)
    probe = tmp_path / "probe.sh"
    probe.write_text(PROBE_STUB, encoding="utf-8")
    probe.chmod(probe.stat().st_mode | stat.S_IXUSR)
    calls = tmp_path / "calls"
    calls.write_text("", encoding="utf-8")
    probe_load = tmp_path / "load.json"
    probe_load.write_text('{"requests":[]}', encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "VAST_API_KEY": "fake",
        "BOOK_TRANSLATOR_CLOUD_LIB_DIR": str(lib),
        "CLOUD_LLM_ENGINE": engine,
        "CLOUD_LLM_TEST_TRANSLATE_CMD": str(translate),
        "CLOUD_LLM_TEST_PROBE_CMD": str(probe),
        "CLOUD_LLM_PROBE_LOAD": str(probe_load),
        "PROBE_CAPTURE": str(tmp_path / "probe-result.json"),
        "TRANSLATE_OUT": str(tmp_path / "translated"),
        "STUB_CALLS": str(calls),
        "STUB_PORT": str(port),
        "CLOUD_LLM_POLL_SECONDS": "1",
        "CLOUD_LLM_BOOT_WAIT_MIN": "1",
    })
    return env


def _run(env: dict[str, str], *args: str):
    cli_args = list(args) if args else ["--", "--book", "x.epub"]
    return subprocess.run(
        ["/bin/bash", str(SCRIPT), *cli_args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_missing_book_stops_before_provider_calls(tmp_path: Path) -> None:
    env = _env(tmp_path, 1, "vllm")
    result = _run(env, "--", "--out", "o")
    assert result.returncode != 0
    assert "找不到 --book" in result.stderr
    assert Path(env["STUB_CALLS"]).read_text(encoding="utf-8") == ""


def test_probe_load_builder_failure_stops_before_provider_calls(tmp_path: Path) -> None:
    env = _env(tmp_path, 1, "vllm")
    env.pop("CLOUD_LLM_PROBE_LOAD")
    env["CLOUD_LLM_TEST_BUILD_LOAD_CMD"] = "false"
    result = _run(env)
    assert result.returncode != 0
    assert "尚未租機" in result.stderr
    assert Path(env["STUB_CALLS"]).read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    ("book_args", "expected"),
    [(["--book=first.epub", "--book", "second.epub"], "first.epub"),
     (["--book", "first.epub", "second.epub"], "first.epub")],
)
def test_probe_load_uses_first_book_argument(
    tmp_path: Path, book_args: list[str], expected: str
) -> None:
    server, port = _serve()
    try:
        env = _env(tmp_path, port, "vllm")
        env.pop("CLOUD_LLM_PROBE_LOAD")
        builder = tmp_path / "builder.sh"
        builder.write_text(
            '#!/usr/bin/env bash\nwhile [[ $# -gt 0 ]]; do case "$1" in --book) echo "$2" >"$BOOK_CAPTURE"; shift 2;; --out) printf \'{"requests":[]}\' >"$2"; shift 2;; *) shift;; esac; done\n',
            encoding="utf-8",
        )
        builder.chmod(builder.stat().st_mode | stat.S_IXUSR)
        env["CLOUD_LLM_TEST_BUILD_LOAD_CMD"] = str(builder)
        env["BOOK_CAPTURE"] = str(tmp_path / "book.txt")
        result = _run(env, "--", *book_args)
        assert result.returncode == 0, result.stderr
        assert Path(env["BOOK_CAPTURE"]).read_text(encoding="utf-8").strip() == expected
    finally:
        server.shutdown()


@pytest.mark.parametrize("engine", ["vllm", "sglang"])
def test_engine_runs_thinking_probe_before_translator_without_external_checkout(
    tmp_path: Path, engine: str
) -> None:
    server, port = _serve()
    try:
        env = _env(tmp_path, port, engine)
        result = _run(env)
        assert result.returncode == 0, result.stderr
        assert len(server.RequestHandlerClass.requests) == 1
        assert server.RequestHandlerClass.requests[0]["chat_template_kwargs"] == {
            "enable_thinking": False
        }
        translated_args = Path(env["TRANSLATE_OUT"]).read_text(encoding="utf-8")
        assert "--concurrent-chapters --max-concurrent-requests 16" in translated_args
        calls = Path(env["STUB_CALLS"]).read_text(encoding="utf-8")
        assert "destroyed" in calls
        if engine == "sglang":
            assert "--reasoning-parser qwen3" in calls
            assert "--max-running-requests 24" in calls
            assert "--max-model-len" not in calls
        else:
            assert "--max-num-seqs 24" in calls
            assert "--max-running-requests" not in calls
    finally:
        server.shutdown()


@pytest.mark.parametrize("engine", ["vllm", "sglang"])
def test_concurrency_override_stays_synchronized_without_external_checkout(
    tmp_path: Path, engine: str
) -> None:
    server, port = _serve()
    try:
        env = _env(tmp_path, port, engine)
        env["CLOUD_LLM_CONCURRENCY"] = "12"
        result = _run(env)
        assert result.returncode == 0, result.stderr
        translated_args = Path(env["TRANSLATE_OUT"]).read_text(encoding="utf-8")
        assert "--max-concurrent-requests 12" in translated_args
        calls = Path(env["STUB_CALLS"]).read_text(encoding="utf-8")
        server_flag = "--max-num-seqs 24" if engine == "vllm" else "--max-running-requests 24"
        assert server_flag in calls
    finally:
        server.shutdown()


@pytest.mark.parametrize("safe", [24, 16, 12, 8])
def test_adaptive_branches_are_covered_without_external_checkout(
    tmp_path: Path, safe: int
) -> None:
    server, port = _serve()
    try:
        env = _env(tmp_path, port, "vllm")
        env["FAKE_SAFE_CONCURRENCY"] = str(safe)
        env["FAKE_PROBE_PASSED"] = "false" if safe == 8 else "true"
        result = _run(env)
        assert result.returncode == 0, result.stderr
        translated = Path(env["TRANSLATE_OUT"]).read_text(encoding="utf-8")
        assert f"--max-concurrent-requests {safe}" in translated
        telemetry = json.loads(Path(env["PROBE_CAPTURE"]).read_text(encoding="utf-8"))
        assert telemetry["gpu"] == "RTX 5090" and telemetry["profile"] == "int4"
        assert telemetry["waves"][0]["mean_single_tok_s"] == 28.5
        if safe == 8:
            assert "都未達 20% 跑飛餘裕" in result.stderr
    finally:
        server.shutdown()


def test_probe_failure_fallback_is_nonfatal_without_external_checkout(tmp_path: Path) -> None:
    server, port = _serve()
    try:
        env = _env(tmp_path, port, "vllm")
        env["FAKE_PROBE_FAIL"] = "1"
        result = _run(env)
        assert result.returncode == 0, result.stderr
        translated = Path(env["TRANSLATE_OUT"]).read_text(encoding="utf-8")
        assert "--max-concurrent-requests 16" in translated
        assert "探針失敗" in result.stderr
    finally:
        server.shutdown()


@pytest.mark.parametrize(("explicit", "safe", "warns"), [(32, 12, True), (8, 12, False)])
def test_explicit_value_still_probes_and_only_warns_above_safe(
    tmp_path: Path, explicit: int, safe: int, warns: bool
) -> None:
    server, port = _serve()
    try:
        env = _env(tmp_path, port, "vllm")
        env["CLOUD_LLM_CONCURRENCY"] = str(explicit)
        env["FAKE_SAFE_CONCURRENCY"] = str(safe)
        result = _run(env)
        assert result.returncode == 0, result.stderr
        assert Path(env["PROBE_CAPTURE"]).exists()
        translated = Path(env["TRANSLATE_OUT"]).read_text(encoding="utf-8")
        assert f"--max-concurrent-requests {explicit}" in translated
        warning = f"明傳併發 {explicit} 高於探針安全值 {safe}"
        assert (warning in result.stderr) is warns
        calls = Path(env["STUB_CALLS"]).read_text(encoding="utf-8")
        assert f"--max-num-seqs {max(24, explicit)}" in calls
    finally:
        server.shutdown()


@pytest.mark.parametrize("engine", ["vllm", "sglang"])
@pytest.mark.parametrize(
    ("content", "reasoning", "expected"),
    [
        ("<think>secret</think>譯文", None, "content 含 think 標籤"),
        ("譯文", False, "reasoning_content 非空"),
    ],
)
def test_engine_thinking_probe_fails_closed_without_external_checkout(
    tmp_path: Path, engine: str, content: object, reasoning: object, expected: str
) -> None:
    server, port = _serve(content=content, reasoning=reasoning)
    try:
        env = _env(tmp_path, port, engine)
        result = _run(env)
        assert result.returncode != 0
        assert expected in result.stderr
        assert not Path(env["TRANSLATE_OUT"]).exists()
        assert "destroyed" in Path(env["STUB_CALLS"]).read_text(encoding="utf-8")
    finally:
        server.shutdown()

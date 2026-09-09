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


def _env(tmp_path: Path, port: int, engine: str) -> dict[str, str]:
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "vast_instance_lib.sh").write_text(VAST_STUB, encoding="utf-8")
    translate = tmp_path / "translate.sh"
    translate.write_text(TRANSLATE_STUB, encoding="utf-8")
    translate.chmod(translate.stat().st_mode | stat.S_IXUSR)
    calls = tmp_path / "calls"
    calls.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "VAST_API_KEY": "fake",
        "BOOK_TRANSLATOR_CLOUD_LIB_DIR": str(lib),
        "CLOUD_LLM_ENGINE": engine,
        "CLOUD_LLM_TEST_TRANSLATE_CMD": str(translate),
        "TRANSLATE_OUT": str(tmp_path / "translated"),
        "STUB_CALLS": str(calls),
        "STUB_PORT": str(port),
        "CLOUD_LLM_POLL_SECONDS": "1",
        "CLOUD_LLM_BOOT_WAIT_MIN": "1",
    })
    return env


def _run(env: dict[str, str]):
    return subprocess.run(
        ["/bin/bash", str(SCRIPT), "--", "--book", "x.epub"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


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
        assert Path(env["TRANSLATE_OUT"]).exists()
        calls = Path(env["STUB_CALLS"]).read_text(encoding="utf-8")
        assert "destroyed" in calls
        if engine == "sglang":
            assert "--reasoning-parser qwen3" in calls
            assert "--max-model-len" not in calls
        else:
            assert "--max-num-seqs" in calls
            assert "--max-running-requests" not in calls
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

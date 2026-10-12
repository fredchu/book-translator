"""Shell 腳本裡 `$變數` 後面緊接中文字，bash 會把中文字的位元組吃進變數名稱。

例：`die "...：$N（尚未開機）"` 印出亂碼並讓 pytest 讀 stderr 時 UnicodeDecodeError；
`log "=== $C：..."` 在 set -u 下直接 `C�: unbound variable` 中止腳本。
2026-10-12 同一天踩兩次，改成測試擋：一律寫 `${變數}`。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PATTERN = re.compile(rb"\$[A-Za-z_][A-Za-z_0-9]*[\x80-\xff]")


def test_no_shell_variable_directly_followed_by_non_ascii() -> None:
    hits = []
    for script in sorted((REPO / "scripts").glob("*.sh")):
        for number, line in enumerate(script.read_bytes().splitlines(), start=1):
            if line.lstrip().startswith(b"#"):
                continue
            if PATTERN.search(line):
                hits.append(f"{script.name}:{number}: {line.decode('utf-8', 'replace').strip()[:120]}")
    assert not hits, "變數後面緊接非 ASCII 字元，改寫成 ${變數}：\n" + "\n".join(hits)

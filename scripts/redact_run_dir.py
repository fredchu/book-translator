#!/usr/bin/env python3
"""把 run 目錄裡的憑證塗掉，供「複製進 git 倉庫之前」使用。

2026-09-10 安全審查：`endpoint.env`／`server-args.txt`／`vast-instance-record.json`
的真值隨 snapshot 被複製進協作倉庫。`runs/` 本身有進 .gitignore，所以漏的不是
runs/，是**把 run 目錄複製到別處**那一步。

`endpoint.env` 跟另外兩個不同：下游（adapter.sh、sweep）執行時真的要讀那把金鑰，
所以不能在寫入時就塗——只能在離開 `runs/` 之前塗。這支就是那一步。

用法：python3 scripts/redact_run_dir.py <目錄>   （就地改，冪等）
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

R = "<redacted>"


def redact_dir(d: Path) -> list[str]:
    touched: list[str] = []

    p = d / "endpoint.env"
    if p.is_file():
        new = re.sub(r"(OMLX_API_KEY=).*", r"\1" + R, p.read_text())
        if new != p.read_text():
            p.write_text(new)
            touched.append(p.name)

    p = d / "server-args.txt"
    if p.is_file():
        lines = p.read_text().splitlines()
        for i, line in enumerate(lines):
            if line.strip() == "--api-key" and i + 1 < len(lines) and lines[i + 1] != R:
                lines[i + 1] = R
                touched.append(p.name)
        p.write_text("\n".join(lines) + "\n")

    p = d / "vast-instance-record.json"
    if p.is_file():
        rec = json.loads(p.read_text())
        if rec.get("jupyter_token") not in (None, R):
            rec["jupyter_token"] = R
            touched.append(p.name)
        args = rec.get("image_args") or []
        for i, v in enumerate(args):
            if v == "--api-key" and i + 1 < len(args) and args[i + 1] != R:
                args[i + 1] = R
                touched.append(p.name)
        p.write_text(json.dumps(rec, ensure_ascii=False))

    return touched


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    d = Path(argv[0])
    if not d.is_dir():
        print(f"不是目錄：{d}", file=sys.stderr)
        return 2
    touched = redact_dir(d)
    print(f"塗掉 {len(set(touched))} 個檔：{sorted(set(touched)) or '（本來就沒有憑證）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""bake.py — 用 headless Chrome 跑 bake.html（內含 face_render.js），把輸出的 JSON
烘焙結果用 face_pack.pack() 封裝成 MFC1 二進位檔。
"""
from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from pathlib import Path

import face_pack

DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DEFAULT_OUT = "tools/puck_face/out/puck_face.bin"

_OUT_RE = re.compile(r'<pre id="out">(.*?)</pre>', re.S)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chrome", default=DEFAULT_CHROME)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    bake_html = (Path(__file__).parent / "bake.html").resolve()
    result = subprocess.run(
        [
            args.chrome,
            "--headless=new",
            "--disable-gpu",
            "--allow-file-access-from-files",
            "--virtual-time-budget=60000",
            "--dump-dom",
            f"file://{bake_html}",
        ],
        capture_output=True,
        timeout=300,
    )

    stdout = result.stdout.decode("utf-8", errors="replace")
    m = _OUT_RE.search(stdout)
    if not m:
        sys.stderr.write(result.stderr.decode("utf-8", errors="replace")[:500])
        return 1

    raw = html.unescape(m.group(1))
    try:
        baked = json.loads(raw)
    except json.JSONDecodeError:
        sys.stderr.write(raw[:500])
        return 1

    data = face_pack.pack(baked)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)

    print(face_pack.summary(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())

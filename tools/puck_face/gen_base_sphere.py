#!/usr/bin/env python3
"""gen_base_sphere.py — 用 headless Chrome 跑 pixel_mockup.html 裡的 drawSphere，輸出球體底圖
（無眼眉、RGB888 240x240）成 C 陣列 firmware/face_demo/base_sphere.h（產物不進 git）。
"""
import base64
import re
import subprocess
import sys
import tempfile
from pathlib import Path

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "firmware/face_demo/base_sphere.h"


def main() -> int:
    src = (ROOT / "tools/puck_face/pixel_mockup.html").read_text(encoding="utf-8")
    ds = re.search(r"function drawSphere\(r\)\{.*?\n\}", src, re.S).group(0)
    page = f"""<!doctype html><meta charset="utf-8"><pre id="out"></pre><script>
const W=240,C=120,R=108,P2=Math.PI*2;
{ds}
const cv=document.createElement('canvas');cv.width=cv.height=W;const r=cv.getContext('2d');drawSphere(r);
const d=r.getImageData(0,0,W,W).data;let s='';
for(let i=0;i<W*W;i++)s+=String.fromCharCode(d[i*4],d[i*4+1],d[i*4+2]);
document.getElementById('out').textContent=btoa(s);
</script>"""
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
        f.write(page)
    dom = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--dump-dom", f"file://{f.name}"],
                         capture_output=True, timeout=120).stdout.decode()
    m = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
    if not m:
        print("Chrome 沒輸出", file=sys.stderr)
        return 1
    b = base64.b64decode(m.group(1))
    with OUT.open("w") as o:
        o.write("// 球體底圖（無眼眉），tools/puck_face/gen_base_sphere.py 產生，RGB888 240x240\n#pragma once\n#include <stdint.h>\n")
        o.write(f"const uint8_t BASE_SPHERE[{len(b)}] = {{\n")
        for i in range(0, len(b), 48):
            o.write(",".join(str(x) for x in b[i:i + 48]) + ",\n")
        o.write("};\n")
    print(f"wrote {OUT} ({len(b)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

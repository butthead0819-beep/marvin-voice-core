#!/usr/bin/env python3
"""build.py — puck 頭像服裝底圖：列清單 / 預覽聯絡表 / 產生韌體 header。

服裝定義在 costumes.js（共用畫筆 helpers.js），用 headless Chrome 畫，眼睛眉線用
tools/puck_face/face_pixel.js（韌體 face_rt.h 的規格來源）疊上去，預覽再模擬 RGB565＋
4×4 Bayer 抖色與 240px 圓屏，跟實機看到的一致。

用法（在 repo 根目錄）：
  python3 tools/puck_face/costume/build.py list
  python3 tools/puck_face/costume/build.py preview [id ...] [--out DIR]
  python3 tools/puck_face/costume/build.py header id [id ...] --out firmware/face_demo/costume_preview.h
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PER_SHEET = 8

# 預覽的姿勢：涵蓋眼睛會跑到的極端位置（沉思×1.3＝眉線最高 y≈70、點頭＝眼睛最低）
POSES = [
    ("靜止", dict(yaw=0, pitch=0, roll=0, browUp=0)),
    ("思考", dict(yaw=0.45, pitch=-0.3, roll=-0.14, browUp=0.04)),
    ("沉思×1.3", dict(yaw=0.52, pitch=-0.585, roll=-0.21, browUp=0.05)),
    ("點頭", dict(yaw=0, pitch=0.42, roll=0.06, browUp=0)),
    ("瞥視", dict(yaw=-1.1, pitch=0, roll=-0.1, browUp=0)),
]
NIGHT = 60 / 200   # face_backlight：夜間 60、白天 200


def _renderer_js() -> str:
    src = (ROOT / "tools/puck_face/face_pixel.js").read_text(encoding="utf-8")
    old = "const IDLE = [104, 158, 58], ERR = [230, 90, 55];"
    if old not in src:
        raise SystemExit("face_pixel.js 的 IDLE 宣告變了，更新 build.py 的替換字串")
    src = src.replace(old, "let IDLE = [104, 158, 58]; const ERR = [230, 90, 55];")
    return ("const FACE = (function(){var module={exports:{}};" + src +
            "\nmodule.exports.setIdle = c => { IDLE = c; };\nreturn module.exports;})();")


def _textures_js() -> str:
    parts = []
    for p in sorted((HERE / "textures").glob("*")):
        if p.suffix.lower() not in (".jpg", ".png"):
            continue
        mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
        parts.append(f"{json.dumps(p.stem)}:'data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}'")
    return "const TEX_SRC={" + ",".join(parts) + "};"


def _run(main_js: str) -> dict:
    page = "\n".join([
        "<!doctype html><meta charset=utf-8><pre id=out></pre><script>",
        "const W=240,C=120,R=108,P2=Math.PI*2;",
        _renderer_js(),
        (HERE / "helpers.js").read_text(encoding="utf-8"),
        _textures_js(),
        "const TEX={};",
        (HERE / "costumes.js").read_text(encoding="utf-8"),
        "(async()=>{try{",
        "for(const [k,src] of Object.entries(TEX_SRC)){const im=new Image();im.src=src;await im.decode();TEX[k]=im;}",
        main_js,
        "}catch(e){document.getElementById('out').textContent=JSON.stringify({error:String(e&&e.stack||e)});}})();",
        "</script>",
    ])
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(page)
    # headless Chrome 偶爾在 async 主程式跑完前就 dump（pre 是空的），重試幾次
    for _ in range(4):
        dom = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--virtual-time-budget=600000",
                              "--dump-dom", f"file://{f.name}"], capture_output=True, timeout=900).stdout.decode()
        m = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
        if m and m.group(1).strip():
            break
    else:
        raise SystemExit("Chrome 沒輸出（重試 4 次，頁面可能出錯）")
    res = json.loads(html.unescape(m.group(1)))
    if "error" in res:
        raise SystemExit("頁面錯誤：\n" + res["error"])
    return res


_BASE_JS = """
function base888(c){const cv=document.createElement('canvas');cv.width=cv.height=W;const r=cv.getContext('2d');
  H.bg(r);c.draw(r);const d=r.getImageData(0,0,W,W).data,b=new Uint8Array(W*W*3);
  for(let i=0;i<W*W;i++){b[i*3]=d[i*4];b[i*3+1]=d[i*4+1];b[i*3+2]=d[i*4+2];}return b;}
function pick(ids){if(!ids.length)return COSTUMES;const m=new Map(COSTUMES.map(c=>[c.id,c]));
  return ids.map(i=>{if(!m.has(i))throw new Error('沒有這個服裝：'+i);return m.get(i);});}
"""


def cmd_list(_args) -> int:
    res = _run("document.getElementById('out').textContent=JSON.stringify("
               "{list:COSTUMES.map(c=>[c.id,c.cat,c.title,c.eye||null])});")
    for cid, cat, title, eye in res["list"]:
        print(f"{cid:16s} {cat:6s} {title}" + (f"  eye={eye}" if eye else ""))
    return 0


def cmd_preview(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    js = _BASE_JS + f"""
const POSES={json.dumps(POSES, ensure_ascii=False)}, NIGHT={NIGHT}, PER={PER_SHEET};
const BAYER=[0,8,2,10,12,4,14,6,3,11,1,9,15,7,13,5];
const q=(v,b,t)=>{{const L=(1<<b)-1,f=v/255*L,n=Math.floor(f)+((f-Math.floor(f))>t?1:0);return Math.round(Math.min(L,n)/L*255);}};
function screen(out,k){{const im=new ImageData(W,W);for(let y=0;y<W;y++)for(let x=0;x<W;x++){{const i=y*W+x,t=(BAYER[(y&3)*4+(x&3)]+0.5)/16;
  const ins=(x+0.5-120)**2+(y+0.5-120)**2<=14400;im.data[i*4]=ins?q(out[i*3]*k,5,t):0;im.data[i*4+1]=ins?q(out[i*3+1]*k,6,t):0;
  im.data[i*4+2]=ins?q(out[i*3+2]*k,5,t):0;im.data[i*4+3]=255;}}return im;}}
const list=pick({json.dumps(args.ids)}),sheets=[];
for(let s=0;s<list.length;s+=PER){{const part=list.slice(s,s+PER),LW=150,CW=244,cols=POSES.length+1;
  const cv=document.createElement('canvas');cv.width=LW+CW*cols;cv.height=28+part.length*CW;const r=cv.getContext('2d');
  r.fillStyle='#1d1d1f';r.fillRect(0,0,cv.width,cv.height);r.fillStyle='#bbb';r.font='14px sans-serif';
  POSES.forEach((p,j)=>r.fillText(p[0],LW+CW*j+4,19));r.fillText('夜間亮度',LW+CW*POSES.length+4,19);
  part.forEach((c,ri)=>{{const base=base888(c),y0=28+ri*CW;FACE.setIdle(c.eye||[104,158,58]);
    r.fillStyle='#eee';r.font='bold 15px sans-serif';r.fillText(c.title,6,y0+24);
    r.fillStyle='#999';r.font='12px sans-serif';r.fillText(c.id,6,y0+44);r.fillText(c.cat,6,y0+62);
    POSES.forEach((p,j)=>{{const out=new Uint8Array(W*W*3);FACE.renderPixel(out,base,Object.assign({{err:0}},p[1]));
      r.putImageData(screen(out,1),LW+CW*j+2,y0+2);}});
    const out=new Uint8Array(W*W*3);FACE.renderPixel(out,base,Object.assign({{err:0}},POSES[0][1]));
    r.putImageData(screen(out,NIGHT),LW+CW*POSES.length+2,y0+2);}});
  sheets.push(cv.toDataURL('image/png'));}}
document.getElementById('out').textContent=JSON.stringify({{sheets}});
"""
    res = _run(js)
    for i, url in enumerate(res["sheets"], 1):
        p = out / f"costume_sheet_{i}.png"
        p.write_bytes(base64.b64decode(url.split(",", 1)[1]))
        print(p)
    return 0


def cmd_header(args) -> int:
    js = _BASE_JS + f"""
const res={{}};for(const c of pick({json.dumps(args.ids)})){{const b=base888(c);let s='';
  for(let i=0;i<b.length;i++)s+=String.fromCharCode(b[i]);res[c.id]={{b64:btoa(s),eye:c.eye||[104,158,58]}};}}
document.getElementById('out').textContent=JSON.stringify(res);
"""
    res = _run(js)
    out = Path(args.out)
    with out.open("w", encoding="utf-8") as o:
        o.write("// puck 頭像服裝底圖（tools/puck_face/costume/build.py 產生，RGB888 240x240，不進 git）\n"
                "#pragma once\n#include <stdint.h>\n")
        for cid, v in res.items():
            b = base64.b64decode(v["b64"])
            assert len(b) == 240 * 240 * 3
            name = "COSTUME_" + re.sub(r"\W", "_", cid).upper()
            o.write(f"// eye={v['eye']}\nconst uint8_t {name}[{len(b)}] = {{\n")
            for i in range(0, len(b), 48):
                o.write(",".join(str(x) for x in b[i:i + 48]) + ",\n")
            o.write("};\n")
    print(out, out.stat().st_size)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p = sub.add_parser("preview")
    p.add_argument("ids", nargs="*")
    p.add_argument("--out", default="/tmp/puck_costume_preview")
    h = sub.add_parser("header")
    h.add_argument("ids", nargs="+")
    h.add_argument("--out", required=True)
    args = ap.parse_args()
    return {"list": cmd_list, "preview": cmd_preview, "header": cmd_header}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())

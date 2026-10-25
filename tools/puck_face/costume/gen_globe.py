#!/usr/bin/env python3
"""gen_globe.py — 讓 LLM 依名詞畫雪花水晶球擺設（SVG 立牌），存成 .svg 給 build.py --svg 預覽。

LLM 走專案 bus（llm_pool.build_tiered_router 的 analyze 免費池；有付費尾的話 bus 自己守門記帳）。
輸出的 SVG 只當圖片畫（headless Chrome 的 Image），而且先過 _sanitize：有 script/foreignObject/
事件屬性/外部連結的一律拒收。

用法（repo 根目錄）：
  python3 tools/puck_face/costume/gen_globe.py 拉麵 鹹酥雞 機車 --out /tmp/puck_globes
  python3 tools/puck_face/costume/build.py preview --svg /tmp/puck_globes/*.svg --out /tmp/puck_preview
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

SYSTEM = "You are an illustrator who draws tiny snow-globe figurines as clean SVG. Output only the SVG."
PROMPT = """Draw "{noun}" as a miniature figurine that stands inside a snow globe.

Hard rules:
- Output ONE <svg> element only, no prose, no markdown fences.
- <svg xmlns="http://www.w3.org/2000/svg" width="240" height="240" viewBox="0 0 240 240">
- Transparent background: NO background rect, NO ground, NO snow, NO sky, NO frame.
- The figurine stands on an invisible floor at y=186; its lowest point touches y=186.
- Horizontally centred on x=120. Max width 150 px. Max height 140 px (top no higher than y=46).
- Simple, bold, recognisable silhouette; flat shapes with 1-2 linear/radial gradients for volume; dark outline about 1.5 px.
- 8 to 40 shapes. No text, no <script>, no <image>, no external references, no filters.
- Make it charming and a little playful; it will be viewed at 240 px on a small round screen."""

_BAD = re.compile(r"<\s*script|foreignObject|<\s*image|\son\w+\s*=|(?:xlink:)?href\s*=\s*[\"'](?!#)|@import|url\(\s*[\"']?(?!#)", re.I)


def _sanitize(text: str) -> str | None:
    m = re.search(r"<svg\b.*?</svg>", text or "", re.S | re.I)
    if not m:
        return None
    svg = m.group(0)
    if _BAD.search(svg) or len(svg) > 40_000:
        return None
    return svg


async def _gen(router, noun: str, tries: int) -> str | None:
    for _ in range(tries):
        out = await router.analyze(PROMPT.format(noun=noun), caller="puck_globe_gen", system=SYSTEM,
                                   max_tokens=4000, temperature=0.9, timeout=90)
        svg = _sanitize(out or "")
        if svg:
            return svg
    return None


async def main_async(args) -> int:
    from dotenv import load_dotenv
    # worktree 沒有 .env（gitignore）時改用執行目錄（主目錄）的 .env 與程式碼
    root = ROOT if (ROOT / ".env").exists() else Path.cwd()
    load_dotenv(root / ".env")
    sys.path.insert(0, str(root))
    from llm_pool import build_tiered_router
    router = build_tiered_router()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    fails = 0
    for noun in args.nouns:
        for k in range(args.n):
            svg = await _gen(router, noun, args.tries)
            name = noun if args.n == 1 else f"{noun}_{k + 1}"
            if svg:
                (out / f"{name}.svg").write_text(svg, encoding="utf-8")
                print(f"✅ {name}  {len(svg)}B")
            else:
                fails += 1
                print(f"❌ {name}  沒拿到合格 SVG")
    print("用量：", dict(router.usage_by_caller))
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("nouns", nargs="+")
    ap.add_argument("--out", default="/tmp/puck_globes")
    ap.add_argument("--n", type=int, default=1, help="每個名詞生幾張（不確定性是好處）")
    ap.add_argument("--tries", type=int, default=2)
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())

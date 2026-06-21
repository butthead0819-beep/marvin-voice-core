"""B 骨架 end-to-end demo：真實日誌 → 一頁漫畫。核心渲染在 diary_comic.render。

用法：venv_simon/bin/python scripts/diary_comic_demo.py [日誌路徑] [輸出] [畫質] [版面]
  畫質：nano / pro2k / pro4k   版面：auto / slant / webtoon / stack
"""
import base64
import io
import json
import sys
import urllib.request

from PIL import Image

sys.path.insert(0, ".")
from diary_comic.parser import parse_log, dedupe_adjacent, eligible_sessions, session_continuity
from diary_comic.render import render_session

LOG_PATH = "records/chat_summary_log.txt"

QUALITY = {
    "nano":  {"model": "gemini-2.5-flash-image", "size": None, "page": (1080, 1920)},
    "pro2k": {"model": "gemini-3-pro-image",     "size": "2K", "page": (1536, 2732)},
    "pro4k": {"model": "gemini-3-pro-image",     "size": "4K", "page": (2160, 3840)},
}


def _load_key() -> str:
    try:
        for line in open(".env", encoding="utf-8"):
            if line.strip().startswith("GEMINI_PAID_API_KEY="):
                return line.strip().split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return ""


def make_image_fn(key, model, image_size):
    if not key:
        return None

    def _gen(prompt, aspect=None):
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent?key={key}")
        cfg = {}
        if aspect:
            cfg["aspectRatio"] = aspect
        if image_size:
            cfg["imageSize"] = image_size
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        if cfg:
            payload["generationConfig"] = {"imageConfig": cfg}
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            d = json.loads(r.read())
        for p in d["candidates"][0]["content"]["parts"]:
            inl = p.get("inlineData") or p.get("inline_data")
            if inl and inl.get("data"):
                return Image.open(io.BytesIO(base64.b64decode(inl["data"]))).convert("RGB")
        raise RuntimeError("no image in response")

    return _gen


def make_text_fn(key):
    if not key:
        return None

    def _gen(system, user):
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               f"gemini-2.5-flash:generateContent?key={key}")
        body = json.dumps({
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"parts": [{"text": user}]}],
        }).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
        return d["candidates"][0]["content"]["parts"][0]["text"]

    return _gen


def main():
    log_path = sys.argv[1] if len(sys.argv) > 1 else LOG_PATH
    out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/diary_comic_demo.png"
    quality = sys.argv[3] if len(sys.argv) > 3 else "nano"
    layout = sys.argv[4] if len(sys.argv) > 4 else "auto"
    q = QUALITY[quality]

    entries = dedupe_adjacent(parse_log(open(log_path, encoding="utf-8").read()))
    sessions = eligible_sessions(entries)
    print(f"日誌={log_path}　夠料場次={len(sessions)}")
    if not sessions:
        print("沒有夠料的對話場次，今天不出頁。")
        return
    session = sessions[-1]
    key = _load_key()
    print(f"最近場次 {session[0].ts_str} 起，{len(session)} 格，連貫度={session_continuity(session):.2f}"
          f"｜畫質={quality} 版面={layout}｜{'真出圖' if key else '佔位'}")

    page, used, line = render_session(
        session, img_fn=make_image_fn(key, q["model"], q["size"]), text_fn=make_text_fn(key),
        cache_dir="/tmp/diary_comic_cache", page_size=q["page"], variant=quality,
        force_layout=layout)
    page.save(out)
    print(f"  → {out}（版面={used}，馬文：{line[:18]}）")


if __name__ == "__main__":
    main()

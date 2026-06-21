"""本週精華處理器：逐字稿 → 爆笑時刻 → LLM 清理笑點 → 文字 digest + 精華漫畫。

涵蓋：#3 本週精華（文字）+ #1 笑點當漫畫 beat。用法：
  venv_simon/bin/python scripts/weekly_highlights.py [db] [天數] [輸出png]
"""
import base64
import datetime
import io
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request

from PIL import Image

sys.path.insert(0, ".")
from diary_comic.highlight import find_highlights, clean_highlight, highlight_to_entry
from diary_comic.render import render_session


def _key():
    try:
        for line in open(".env", encoding="utf-8"):
            if line.strip().startswith("GEMINI_PAID_API_KEY="):
                return line.strip().split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return ""


def _text_fn(key):
    def gen(system, user):
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               f"gemini-2.5-flash:generateContent?key={key}")
        body = json.dumps({"system_instruction": {"parts": [{"text": system}]},
                           "contents": [{"parts": [{"text": user}]}]}).encode()
        for attempt in range(5):  # 429 退避重試（批次清理會撞每分鐘限速）
            req = urllib.request.Request(url, data=body,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read())["candidates"][0]["content"]["parts"][0]["text"]
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 4:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise
    return gen


def _img_fn(key):
    def gen(prompt, aspect=None):
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               f"gemini-2.5-flash-image:generateContent?key={key}")
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        if aspect:
            payload["generationConfig"] = {"imageConfig": {"aspectRatio": aspect}}
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read())
        for p in d["candidates"][0]["content"]["parts"]:
            inl = p.get("inlineData") or p.get("inline_data")
            if inl and inl.get("data"):
                return Image.open(io.BytesIO(base64.b64decode(inl["data"]))).convert("RGB")
        raise RuntimeError("no image")
    return gen


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else "marvin.db"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    out_png = sys.argv[3] if len(sys.argv) > 3 else "/tmp/weekly_highlights.png"

    con = sqlite3.connect(db)
    cutoff = datetime.datetime.now().timestamp() - days * 86400
    rows = con.execute("SELECT speaker, text, timestamp FROM transcripts "
                       "WHERE timestamp >= ? ORDER BY timestamp ASC", (cutoff,)).fetchall()
    con.close()

    highlights = find_highlights(rows)
    print(f"近 {days} 天 {len(rows)} 句 → {len(highlights)} 個爆笑精華")
    if not highlights:
        return
    key = _key()
    text_fn = _text_fn(key) if key else None

    # 清理每個笑點 → 文字 digest
    print("\n📣 ===== 本週精華 =====")
    cleaned = []
    for h in highlights:
        line = clean_highlight(h, generate_fn=text_fn)
        cleaned.append((h, line))
        time.sleep(0.4)  # throttle，別撞限速
        when = datetime.datetime.fromtimestamp(h.ts).strftime("%m/%d %H:%M")
        print(f"  [{when}] {line}（{h.laugher} 笑爆）")

    # 取最強的 6 個當漫畫 beat → 出一頁精華漫畫
    top = sorted(cleaned, key=lambda c: c[0].strength, reverse=True)[:6]
    top = sorted(top, key=lambda c: c[0].ts)  # 還原時序
    session = [highlight_to_entry(h, core=line) for h, line in top]
    print(f"\n🎨 用最強 {len(session)} 個笑點出精華漫畫...")
    page, layout, marvin = render_session(
        session, img_fn=(_img_fn(key) if key else None), text_fn=text_fn,
        cache_dir="/tmp/diary_comic_cache", variant="nano")
    page.save(out_png)
    print(f"  → {out_png}（版面={layout}，馬文：{marvin[:18]}）")


if __name__ == "__main__":
    main()

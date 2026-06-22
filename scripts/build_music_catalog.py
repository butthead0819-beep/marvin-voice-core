#!/usr/bin/env python3
"""建乾淨 canonical 歌表（fast-path 比對目標）：播放史 → ytmusicapi 結構化「歌手 歌名」。

關鍵（見 memory music_pinyin_fastpath）：**不從 YT 髒 video title 硬解**（試過會爛：
「周杰倫 周杰倫」、把歌詞當歌名）。改用 ytmusicapi search(filter="songs") 回的
結構化 artists[].name + title，天生乾淨。

流程：music_memory songs（key=YT URL）→ is_song 預篩省 API → 用標題當 seed
search → 取 top song 的 {artist, title} → canonical「歌手 歌名」→ 寫
records/music_catalog.json（[{name, pinyin, videoId}]）。

incremental：已解析的（videoId 命中）跳過；每首失敗 graceful 不中斷。
用法：python3 scripts/build_music_catalog.py [--limit N] [--sleep 0.3] [--rebuild]
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

SRC = Path("music_memory.json")
OUT = Path("records/music_catalog.json")

# 明確非歌 marker（預篩，省 API + 不污染目錄）。偏精準關鍵詞，不用結構（長度/pipe）誤殺真歌。
_NON_SONG = [
    "串燒", "串烧", "精選", "精选", "合集", "合輯", "合辑", "金曲串", "排行榜",
    "歌單", "歌单", "playlist", "medley", "純音樂", "纯音乐", "lo-fi", "lofi",
    "睡眠", "助眠", "讀書工作", "读书工作", "背景音樂", "背景音乐", "深夜電台", "深夜电台",
    "綜藝", "综艺", "唱作人", "我是歌手", "純享", "纯享", "舞台纯", "演唱會46", "演唱会46",
    "系統玩", "系统玩", "重生唐", "天驕", "天骄", "开局羞辱", "全網首發", "全网首发",
    "eng sub", "engsub", "財報", "财报", "k線", "k线", "黑馬股", "黑马股",
]


def is_song(title: str) -> bool:
    t = title.lower()
    if any(k.lower() in t for k in _NON_SONG):
        return False
    return (title.count("｜") + title.count("|")) < 3


def seed_from_title(title: str) -> str:
    """YT 標題 → 給 ytmusicapi 的搜尋 seed（粗清即可，search 本身容錯）。"""
    t = re.sub(r"[【】\[\]()（）『』「」]", " ", title)
    t = re.sub(r"Official.*|Music Video|高畫質.*|官方.*|完整版.*|歌詞.*|Lyric.*|"
               r"華納.*|\bHD\b|\bMV\b|\b4K\b|動態歌詞.*|动态歌词.*", " ", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip()[:60]


def clean_canonical_title(title: str) -> str:
    """ytmusicapi 回的 title → 去版本/英譯尾巴（「想你的夜 (未眠版) - Miss You」→「想你的夜」）。"""
    t = re.split(r"\s[-–]\s", title)[0]           # 砍 ' - English' 尾
    t = re.sub(r"[(（【\[].*", "", t)              # 砍版本括號 (未眠版)
    return t.strip() or title.strip()


def _to_pinyin(s: str):
    try:
        from pypinyin import lazy_pinyin
        return " ".join(lazy_pinyin(s)).lower()
    except Exception:
        return None


def _video_id(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|watch\?v=)([\w-]{11})", url)
    return m.group(1) if m else url


def resolve(yt, title: str):
    """搜 ytmusicapi → (canonical_name, clean_title) 或 None。"""
    seed = seed_from_title(title)
    if not seed:
        return None
    res = yt.search(seed, filter="songs", limit=1)
    if not res:
        return None
    top = res[0]
    artist = " ".join(a["name"] for a in (top.get("artists") or []) if a.get("name"))
    ctitle = clean_canonical_title(top.get("title") or "")
    name = f"{artist} {ctitle}".strip()
    return name if len(name) >= 2 else None


def fetch_kkbox(limit: int = 100) -> list[dict]:
    """KKBOX 華語單曲週榜（公開 API，免金鑰）→ [{name, pinyin, source}]。

    補充播放史沒涵蓋的當前熱門華語。category=297=華語、terr=tw。
    結構：data.charts.song[].{artist_name, song_name}。失敗回 []（best-effort）。
    """
    import requests
    url = "https://kma.kkbox.com/charts/api/v1/weekly"
    params = {"category": "297", "lang": "tc", "limit": str(limit),
              "terr": "tw", "type": "song"}
    try:
        r = requests.get(url, params=params, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0"})
        songs = (r.json().get("data") or {}).get("charts", {}).get("song") or []
    except Exception:
        return []
    out = []
    for s in songs:
        artist = (s.get("artist_name") or "").strip()
        title = (s.get("song_name") or "").strip()
        name = f"{artist} {title}".strip()
        if len(name) >= 2:
            out.append({"name": name, "pinyin": _to_pinyin(name) or "", "source": "kkbox"})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只處理前 N 首播放史（測試）")
    ap.add_argument("--sleep", type=float, default=0.3, help="每次 API 間隔秒（限流）")
    ap.add_argument("--rebuild", action="store_true", help="忽略既有快取重建")
    ap.add_argument("--kkbox", type=int, default=0, metavar="N",
                    help="併入 KKBOX 華語週榜 top-N（補當前熱門，0=不抓）")
    args = ap.parse_args()

    from ytmusicapi import YTMusic
    yt = YTMusic()

    songs = json.loads(SRC.read_text(encoding="utf-8"))["songs"]
    cache = {}
    if OUT.exists() and not args.rebuild:
        for row in json.loads(OUT.read_text(encoding="utf-8")):
            if row.get("videoId"):
                cache[row["videoId"]] = row

    items = list(songs.items())
    if args.limit:
        items = items[: args.limit]

    resolved, skipped, failed, dropped = 0, 0, 0, 0
    out = dict(cache)  # videoId → row
    for url, meta in items:
        vid = _video_id(url)
        if vid in cache:
            skipped += 1
            continue
        title = meta.get("title", "")
        if not is_song(title):
            dropped += 1
            continue
        try:
            name = resolve(yt, title)
        except Exception:
            name = None
        if not name:
            failed += 1
            continue
        out[vid] = {"name": name, "pinyin": _to_pinyin(name) or "", "videoId": vid}
        resolved += 1
        if args.sleep:
            time.sleep(args.sleep)

    rows = list(out.values())

    # KKBOX 華語週榜補充（按 canonical name 去重，播放史優先）
    kkbox_added = 0
    if args.kkbox:
        seen_names = {r["name"] for r in rows}
        for kr in fetch_kkbox(args.kkbox):
            if kr["name"] not in seen_names:
                rows.append(kr)
                seen_names.add(kr["name"])
                kkbox_added += 1

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"[catalog] 新解析 {resolved} | 快取跳過 {skipped} | 非歌剔 {dropped} | "
          f"失敗 {failed} | KKBOX 補 {kkbox_added} → 共 {len(rows)} 首 → {OUT}")
    for r in rows[-6:]:
        print(f"    {r['name'][:40]}")


if __name__ == "__main__":
    main()

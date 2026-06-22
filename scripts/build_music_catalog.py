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


# ytmusicapi 回的藝人名英中不一致（同一人有時「蔡依林」有時「Jolin Tsai」）。
# 使用者語音說中文名 → 拼音匹配需中文藝人名。房間英文名藝人別名表（小、可長）。
_ARTIST_ALIAS = {
    "jolin tsai": "蔡依林", "david tao": "陶喆", "khalil fong": "方大同",
    "eric chou": "周興哲", "eason chan": "陳奕迅", "sun yanzi": "孫燕姿",
    "jacky cheung": "張學友", "g.e.m.": "鄧紫棋", "tiger huang": "黃小琥",
    "ricky hsiao": "蕭煌奇", "phil chang": "張宇", "jj lin": "林俊傑",
    "jay chou": "周杰倫", "a-mei": "張惠妹", "wu bai": "伍佰",
}


def _normalize_artist(artist: str) -> str:
    """英文藝人名 → 中文（房間別名表），讓拼音匹配對得上使用者說的中文名。"""
    key = artist.strip().lower()
    return _ARTIST_ALIAS.get(key, artist)


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
    artist = _normalize_artist(" ".join(a["name"] for a in (top.get("artists") or []) if a.get("name")))
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
        artist = _normalize_artist((s.get("artist_name") or "").strip())
        title = (s.get("song_name") or "").strip()
        name = f"{artist} {title}".strip()
        if len(name) >= 2:
            out.append({"name": name, "pinyin": _to_pinyin(name) or "", "source": "kkbox"})
    return out


def collect_artists(rows: list[dict], min_count: int = 2, cap: int = 80) -> list[str]:
    """從目錄名抽房間常點藝人（出現 ≥min_count 次）。name 格式「藝人 歌名」，取首 token。

    多次出現＝房間真愛的藝人 → 值得擴展其碟補沒播過的歌（如蔡依林的倒帶）。
    """
    from collections import Counter
    c = Counter()
    for r in rows:
        a = r.get("artist") or _extract_artist(r.get("name", ""))
        if len(a) >= 2:
            c[a] += 1
    return [a for a, n in c.most_common(cap) if n >= min_count]


def _extract_artist(name: str) -> str:
    """name「藝人 歌名」→ 藝人。CJK 開頭取首 token（周杰倫）；英文開頭取到第一個
    中文 token 前的完整英文名（「Jolin Tsai 倒帶」→「Jolin Tsai」，ytmusicapi 搜得到）。"""
    toks = name.split(" ")
    if not toks or not toks[0]:
        return ""
    if re.match(r"[一-鿿]", toks[0]):
        return toks[0]
    eng = []
    for t in toks:
        if re.match(r"[一-鿿]", t):
            break
        eng.append(t)
    return " ".join(eng)


def expand_artists(yt, artists: list[str], per_artist: int, sleep: float) -> list[dict]:
    """每位藝人 ytmusicapi 抓 top-N 首 → canonical rows（補沒播過的同藝人歌）。"""
    import time as _t
    out = []
    for a in artists:
        try:
            res = yt.search(a, filter="songs", limit=per_artist)
        except Exception:
            continue
        for t in res:
            artist = _normalize_artist(" ".join(x["name"] for x in (t.get("artists") or []) if x.get("name")))
            title = clean_canonical_title(t.get("title") or "")
            name = f"{artist} {title}".strip()
            if len(name) >= 2:
                out.append({"name": name, "pinyin": _to_pinyin(name) or "", "source": "artist_expand"})
        if sleep:
            _t.sleep(sleep)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只處理前 N 首播放史（測試）")
    ap.add_argument("--sleep", type=float, default=0.3, help="每次 API 間隔秒（限流）")
    ap.add_argument("--rebuild", action="store_true", help="忽略既有快取重建")
    ap.add_argument("--kkbox", type=int, default=0, metavar="N",
                    help="併入 KKBOX 華語週榜 top-N（補當前熱門，0=不抓）")
    ap.add_argument("--expand-artists", type=int, default=0, metavar="N",
                    help="藝人擴展：房間常點藝人每人抓 top-N 首（補沒播過的同藝人歌，0=不做）")
    ap.add_argument("--expand-cap", type=int, default=80,
                    help="藝人擴展最多取幾位藝人（依出現頻率）")
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

    seen_names = {r["name"] for r in rows}

    def _merge(new_rows):
        n = 0
        for nr in new_rows:
            if nr["name"] not in seen_names:
                rows.append(nr)
                seen_names.add(nr["name"])
                n += 1
        return n

    # KKBOX 華語週榜補充（補當前熱門）
    kkbox_added = _merge(fetch_kkbox(args.kkbox)) if args.kkbox else 0

    # 藝人擴展（補房間常點藝人沒播過的歌，如蔡依林的倒帶）
    expand_added = 0
    if args.expand_artists:
        artists = collect_artists(rows, cap=args.expand_cap)
        print(f"[expand] 房間藝人 {len(artists)} 位，每人抓 {args.expand_artists} 首…")
        expand_added = _merge(expand_artists(yt, artists, args.expand_artists, args.sleep))

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"[catalog] 新解析 {resolved} | 快取跳過 {skipped} | 非歌剔 {dropped} | "
          f"失敗 {failed} | KKBOX 補 {kkbox_added} | 藝人擴展 {expand_added} → 共 {len(rows)} 首 → {OUT}")
    for r in rows[-6:]:
        print(f"    {r['name'][:40]}")


if __name__ == "__main__":
    main()

"""解析當夜「使用者主動點歌」紀錄（bot log 的 [點歌-手動] / [點歌-語音] 行）→ 漫畫「點歌台」素材。

純函式：吃 log 文字 + 時間窗，回 [(requester, song_title)]。給策展層當一格文字素材。
"""
from __future__ import annotations

import datetime as _dt
import re

# 同時吃 [點歌-手動]（文字/按鈕）與 [點歌-語音]（語音），兩者都是使用者主動點歌。
# 語音行的「搜尋=」可能夾帶 (修正→…) 標記，靠 .+? non-greedy 跨到「| 結果=」。
_LINE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d).*?點歌-(?:手動|語音)\]\s*使用者=(.+?)\s*\|\s*搜尋=.+?\s*\|\s*結果=(.+?)\s*/")


def _clean_title(title: str) -> str:
    """精簡歌名：砍掉 Official/MV/動態歌詞/『歌詞引言』/｜分隔等贅詞，留主體。
    保守只切明確贅詞分隔符（不含「」，避免砍掉以引號為名的歌）。"""
    t = re.split(r"[\(（【\[『｜|]", title)[0].strip()
    return (t or title).strip()


def parse_manual_requests(log_text: str, since: float | None = None,
                          until: float | None = None) -> list[tuple[str, str]]:
    """回時間窗內的 [(點歌者, 完整歌名)]，依時間序。完整歌名供對 music_memory 取縮圖；
    顯示時再用 clean_title 精簡。since/until 為 epoch 秒，None=不限。"""
    out: list[tuple[str, str]] = []
    for line in log_text.splitlines():
        m = _LINE.search(line)
        if not m:
            continue
        ts_str, user, title = m.groups()
        try:
            ts = _dt.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            continue
        if since is not None and ts < since:
            continue
        if until is not None and ts > until:
            continue
        out.append((user.strip(), title.strip()))
    return out


def clean_title(title: str) -> str:
    """顯示用精簡歌名。"""
    return _clean_title(title)


def video_id_from_url(url: str) -> str | None:
    m = re.search(r"(?:v=|youtu\.be/)([\w-]{11})", url or "")
    return m.group(1) if m else None


def build_title_index(songs: dict) -> dict:
    """music_memory['songs']（url→{title,webpage_url,…}）→ {完整歌名: video_id}。"""
    idx: dict[str, str] = {}
    for url, v in (songs or {}).items():
        if isinstance(v, dict) and v.get("title"):
            vid = video_id_from_url(v.get("webpage_url") or url)
            if vid:
                idx[v["title"]] = vid
    return idx


def thumb_url(video_id: str) -> str:
    return f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"


def dj_tally(requests: list[tuple[str, str]]) -> list[tuple[str, int]]:
    """誰點最多 → [(點歌者, 次數)] 由多到少。"""
    counts: dict[str, int] = {}
    for user, _ in requests:
        counts[user] = counts.get(user, 0) + 1
    return sorted(counts.items(), key=lambda x: (-x[1], x[0]))

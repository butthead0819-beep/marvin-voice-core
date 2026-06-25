"""讀 records/themed_sets.jsonl → 當夜「今夜歌單」素材（Marvin 策展的主題歌單卡用）。

純函式：吃 jsonl 文字 + 時間窗，回 [ThemedSetRecord]。資料源異於點歌台
（song_requests 解 bot log 的使用者主動點歌）——這裡是 Marvin 自己策展的成塊歌單。
"""
from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass
class ThemedSetRecord:
    ts: float
    theme_title: str
    picks: list  # [{"title","reason","url"}]


def parse_themed_sets(text: str, since: float | None = None,
                      until: float | None = None) -> list[ThemedSetRecord]:
    """每行一筆 themed set record，回時間窗內的 [ThemedSetRecord]，依出現（時間）序。

    壞行 / 無 ts / 無 title / 無有效 picks → 跳過。since/until 為 epoch 秒，None=不限。
    """
    out: list[ThemedSetRecord] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        ts = d.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        if since is not None and ts < since:
            continue
        if until is not None and ts > until:
            continue
        title = str(d.get("theme_title") or "").strip()
        picks = [p for p in (d.get("picks") or [])
                 if isinstance(p, dict) and str(p.get("title") or "").strip()]
        if not title or not picks:
            continue
        out.append(ThemedSetRecord(ts=float(ts), theme_title=title, picks=picks))
    return out


def latest_themed_set(text: str, since: float | None = None,
                      until: float | None = None) -> ThemedSetRecord | None:
    """時間窗內最後一張主題歌單（一晚可能多張 → 取最後，最貼合那晚收尾的氣氛）。無→None。"""
    sets = parse_themed_sets(text, since, until)
    return sets[-1] if sets else None

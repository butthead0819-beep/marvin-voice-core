"""LRC parsing + fragment locator — pure functions for「找歌詞 X」timestamp seek.

LRC 格式：每行 `[mm:ss.xx]歌詞文字`。Metadata 行 `[ti:...]` `[ar:...]` 略過。

設計取捨：MVP exact substring match only。STT typo（「青」聽成「清」）會 miss，那是
fallback 路徑的事；這個模組職責單一：把 LRC 視為 ground truth，能找就找、找不到回 None。
fuzzy match 之後若 LRC 命中率不夠再加。
"""
from __future__ import annotations

import re

_LINE_RE = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\](.*)$")


def parse_lrc(lrc: str) -> list[tuple[float, str]]:
    """Parse LRC string → list of (timestamp_seconds, line_text).

    Metadata 行（[ti:...] [ar:...] 等沒有 mm:ss 格式）自動略過。
    純文字行（沒 timestamp 標記）也略過。
    """
    if not lrc:
        return []
    out: list[tuple[float, str]] = []
    for raw in lrc.splitlines():
        m = _LINE_RE.match(raw.strip())
        if not m:
            continue
        minutes = int(m.group(1))
        seconds = float(m.group(2))
        text = m.group(3).strip()
        out.append((minutes * 60 + seconds, text))
    return out


def find_lyrics_timestamp(lrc: str, fragment: str) -> tuple[float, str] | None:
    """在 LRC 內找 fragment 第一次出現的時間戳。

    回傳 (timestamp_seconds, matched_line_text)，沒命中回 None。
    匹配前先把 LRC 行與 fragment 的所有空白移除，所以 STT 偶爾插空白 / LRC 空白格式不一
    都能容錯。
    """
    if not fragment or not fragment.strip():
        return None
    needle = re.sub(r"\s+", "", fragment)
    if not needle:
        return None
    for ts, line in parse_lrc(lrc):
        if not line:
            continue
        haystack = re.sub(r"\s+", "", line)
        if needle in haystack:
            return ts, line
    return None

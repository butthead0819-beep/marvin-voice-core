"""DJ 口白超長時先拿掉「上一首」的子句，再交給 truncate_for_tts 從尾巴截斷。

2026-09-18 使用者要求：口白若被過長的 YouTube 標題吃掉字數，就不要唸前一首，
只要口白 + 下一首。截斷是從尾巴砍，而口白尾巴通常是在帶下一首，所以先砍上一首。
"""
from __future__ import annotations

import re
from typing import Callable

from song_name_clean import clean_title_regex
from tts_length_policy import _CLOSING_QUOTES, _SENTENCE_END_CHARS, truncate_for_tts

_CJK_RUN = re.compile(r"[一-鿿]+")
_ASCII_WORD = re.compile(r"[A-Za-z]+")
_CLAUSE_SPLIT = re.compile(r'[^，,。！？!?；;]+[，,。！？!?；;]?')

_STOPWORDS = {
    "official", "music", "video", "lyrics", "lyric", "feat", "version",
    "live", "audio", "remix", "cover", "topic",
}


def name_keys(name: str) -> set[str]:
    """從歌名抽出比對用的 key（CJK 三字連續子字串 / 雙字整段 + 英文長單字）。"""
    cleaned = clean_title_regex(name)
    if not cleaned:
        return set()

    keys: set[str] = set()

    for run in _CJK_RUN.findall(cleaned):
        if len(run) == 2:
            keys.add(run)
        elif len(run) >= 3:
            for i in range(len(run) - 2):
                keys.add(run[i:i + 3])

    for word in _ASCII_WORD.findall(cleaned):
        lw = word.lower()
        if len(lw) >= 4 and lw not in _STOPWORDS:
            keys.add(lw)

    return keys


def strip_prev_song_mention(text: str, prev_title: str, next_title: str) -> str:
    """把提到「上一首」但沒提到「下一首」的子句拿掉；抓不到就回原文（fail-safe）。"""
    prev_keys = name_keys(prev_title)
    if not prev_keys:
        return text

    next_keys = name_keys(next_title)

    clauses = _CLAUSE_SPLIT.findall(text)

    def _mentions(clause: str, keys: set[str]) -> bool:
        lc = clause.lower()
        return any(k in lc for k in keys)

    kept = [
        clause for clause in clauses
        if not (_mentions(clause, prev_keys) and not _mentions(clause, next_keys))
    ]

    result = "".join(kept).lstrip("，,。！？!?；; 　")

    if len(result.strip()) < 10 or result == text:
        return text

    return result


def gate_dj_intro(
    text: str,
    prev_title: str,
    next_title: str,
    task: str,
    estimate_fn: Callable[[str], float],
) -> tuple[str, bool, bool]:
    """超長時先拿掉上一首再截斷。回傳 (final_text, was_cut, prev_dropped)。"""
    gated, was_cut = truncate_for_tts(text, task, estimate_fn)
    if not was_cut or not prev_title:
        return gated, was_cut, False

    trimmed = strip_prev_song_mention(text, prev_title, next_title)
    if trimmed == text:
        return gated, was_cut, False

    gated, was_cut = truncate_for_tts(trimmed, task, estimate_fn)
    return gated, was_cut, True


def mentions_title(text: str, title: str) -> bool:
    """text 有沒有提到 title（用 name_keys 比對，不分大小寫）。

    給 Consistency Guard 用：口白沒提到上一首，佇列順序變了也不必丟。title 抽不出
    任何 key 時無法判斷 → 回 True（保守：維持 Guard 比對，寧可多擋不唸錯歌名）。
    """
    keys = name_keys(title)
    if not keys:
        return True
    lc = (text or "").lower()
    return any(k in lc for k in keys)


def trim_to_last_sentence(text: str, max_chars: int = 120, min_chars: int = 30) -> str | None:
    """超過 max_chars 時，截到 max_chars 內最後一個句尾符號（含緊接的收尾引號）。

    截完 < min_chars 或找不到句尾符號 → None（交給呼叫端走原本的退路）。未超長原樣回傳。
    """
    if len(text) <= max_chars:
        return text
    for i in range(max_chars - 1, -1, -1):
        if text[i] in _SENTENCE_END_CHARS:
            j = i + 1
            while j < len(text) and text[j] in _CLOSING_QUOTES:
                j += 1
            out = text[:j].strip()
            return out if len(out) >= min_chars else None
    return None

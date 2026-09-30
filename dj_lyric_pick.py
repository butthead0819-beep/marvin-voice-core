"""9/30 使用者定：歌詞槽＝老朋友「想跟你分享這首的原因」——重複最多的句子
≈副歌，只用真實抓到的歌詞本地挑一句，不讓 LLM 編歌詞。
"""
from __future__ import annotations

import re

_MIN_LINE_LEN = 6
_MAX_LINE_LEN = 30
_CREDIT_KEYWORDS = (
    "作詞", "作曲", "編曲", "製作", "監製", "演唱", "詞：", "曲：",
    "lyricist", "composer", "producer",
)
# 段落標記（[CHORUS]、(Verse 2)、【副歌】）與純哼唱（Oh-oh、la la、yeah）不是能分享的歌詞（9/30 實測挑到）
_SECTION_TAG = re.compile(r"^[\[(（【].*[\])）】]$")
_VOCABLES = {"oh", "ooh", "ah", "la", "na", "yeah", "hey", "woo", "whoa", "uh", "mm", "hmm", "da", "ha"}


def _is_filler(line: str) -> bool:
    if _SECTION_TAG.match(line):
        return True
    if re.search(r"[\u4e00-\u9fff]", line):
        return False
    words = re.findall(r"[a-z]+", line.lower())
    return bool(words) and all(w in _VOCABLES for w in words)


def pick_chorus_line(lyrics: str | None) -> str | None:
    if not lyrics:
        return None

    counts: dict[str, int] = {}
    order: list[str] = []
    for raw_line in lyrics.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if len(line) < _MIN_LINE_LEN or len(line) > _MAX_LINE_LEN:
            continue
        if "：" in line or ":" in line:
            continue
        lowered = line.lower()
        if any(kw.lower() in lowered for kw in _CREDIT_KEYWORDS):
            continue
        if _is_filler(line):
            continue
        if line not in counts:
            counts[line] = 0
            order.append(line)
        counts[line] += 1

    best_line = None
    best_count = 1
    for line in order:
        c = counts[line]
        if c > best_count:
            best_count = c
            best_line = line

    return best_line

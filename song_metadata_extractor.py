"""YouTube 歌曲簡介本地正則抽取器。

純本地字串處理，零網路 I/O、零 API 成本：
1. 抽取作詞（Lyricist）、作曲（Composer）、編曲（Arranger）、製作人（Producer）
2. 抽取收錄專輯（Album）
3. 提煉官方文案與創作背景精華（排除標題行、工作人員名單、社群連結與版權宣傳）
"""
from __future__ import annotations

import re

# 排除噪訊行的關鍵字
_NOISE_LINE_KEYWORDS = (
    "導演", "director", "攝影", "cinematographer", "剪輯", "editor",
    "化妝", "makeup", "造型", "styling", "服裝", "costume",
    "燈光", "gaffer", "美術", "art director", "調光", "colorist",
    "企劃", "planner", "專案", "project", "文案", "copywriter",
    "op:", "sp:", "isrc", "publishing",
    "http://", "https://", "linktr.ee", "bit.ly",
    "訂閱", "subscribe", "關注", "follow", "追蹤",
    "instagram", "facebook", "tiktok", "weibo", "twitter",
    "kkbox", "spotify", "apple music", "youtube music", "mytrend",
    "數位平台", "全面上線", "鈴聲下載", "原聲帶購買",
    "版權所有", "翻印必究", "all rights reserved",
    "official music video", "official mv", "official video",
    "官方完整版", "官方 mv", "完整版 mv", "hd", "4k", "1080p",
)

# 排除標題行模式
_TITLE_LINE_PATTERN = re.compile(
    r"^(?:.+[【《\[].+[】》\]]|.+official.+|.+mv\b|.+music video\b)",
    re.IGNORECASE,
)

# 詞曲編製開頭的行
_CREDITS_LINE_PATTERN = re.compile(
    r"^(?:作\s*詞|填\s*詞|作\s*曲|譜\s*曲|編\s*曲|編\s*配|製\s*作|監\s*製|混\s*音|母\s*帶|錄\s*音|吉\s*他|貝\s*斯|鼓|合\s*聲|弦\s*樂|鋼\s*琴|收\s*錄|專\s*輯|lyrics|music|composer|producer|arranged|album)[：:\s]",
    re.IGNORECASE,
)

# 詞曲編製與專輯正則模式
_PATTERNS = {
    "lyricist": [
        r"(?im)^[ \t]*(?:作\s*詞|填\s*詞|Lyricist|Lyrics(?:\s*by)?|Words(?:\s*by)?)[：:\s]+([^\n\r]+?)(?=\s*(?:作曲|編曲|製作|Produced|Music|Arranged|http|$|\n))",
    ],
    "composer": [
        r"(?im)^[ \t]*(?:作\s*曲|譜\s*曲|Composer|Composed\s*by|Music(?:\s*by|\s*[:：]))[：:\s]*([^\n\r]+?)(?=\s*(?:作詞|編曲|製作|Produced|Lyrics|Arranged|http|$|\n))",
    ],
    "arranger": [
        r"(?im)^[ \t]*(?:編\s*曲|編\s*配|Arrangement(?:\s*by)?|Arranged\s*by|Arranger)[：:\s]+([^\n\r]+?)(?=\s*(?:作詞|作曲|製作|Produced|Lyrics|Music|http|$|\n))",
    ],
    "producer": [
        r"(?im)^[ \t]*(?:製\s*作\s*人|監\s*製|Producer|Produced\s*by)[：:\s]+([^\n\r]+?)(?=\s*(?:作詞|作曲|編曲|Arranged|Lyrics|Music|http|$|\n))",
    ],
    "album": [
        r"(?im)^[ \t]*(?:收錄[於在](?:全新|個人)?(?:專輯)?|專輯(?:名稱)?|From\s*the\s*album|Album)[：:\s]*[《“\"]?([^《》“”\"\n\r]+?)[》”\"]?(?=\s*(?:\(|$|\n))",
    ],
}


def _clean_field_val(val: str) -> str:
    """清理提取出來的名單字串（去除多餘標點符號與括號）。"""
    v = (val or "").strip()
    v = re.sub(r"^[《“\"'(\[]+|[》”\"')\]]+$", "", v).strip()
    v = re.sub(r"\s+", " ", v).strip()
    return v[:80] if len(v) > 80 else v


def clean_story_snippet(text: str) -> str:
    """過濾標題行、工作人員名單、社群連結與版權宣傳，保留核心創作背景與故事文案。"""
    if not text:
        return ""

    lines = text.splitlines()
    meaningful_lines: list[str] = []

    for line in lines:
        l_strip = line.strip()
        if not l_strip or len(l_strip) < 6:
            continue
        l_lower = l_strip.lower()
        if any(kw in l_lower for kw in _NOISE_LINE_KEYWORDS):
            continue
        if _TITLE_LINE_PATTERN.match(l_strip):
            continue
        if _CREDITS_LINE_PATTERN.match(l_strip):
            continue
        meaningful_lines.append(l_strip)

    if not meaningful_lines:
        return ""

    # 取前 1~2 個最有語義的段落
    combined = " ".join(meaningful_lines[:2])
    # 限制在 15-120 字
    return combined[:120].strip()


def extract_song_metadata_from_description(desc: str) -> dict[str, str]:
    """從 YouTube 簡介文字中抽取作詞、作曲、編曲、製作人、專輯及文案精華。"""
    if not desc:
        return {}

    result: dict[str, str] = {}

    for field, patterns in _PATTERNS.items():
        for p in patterns:
            match = re.search(p, desc)
            if match:
                val = _clean_field_val(match.group(1))
                if val and len(val) >= 2 and not any(kw in val.lower() for kw in ("http", "subscribe", "訂閱")):
                    result[field] = val
                    break

    story = clean_story_snippet(desc)
    if story:
        result["story_snippet"] = story

    return result

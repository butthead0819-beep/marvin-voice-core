"""
music_search.py — YouTube 搜尋候選評分與過濾。

目的：marvin_play 用文字搜尋時，避免回傳實況、反應、開箱、podcast 等非音樂內容。
作法：ytsearch5 取 5 個候選 → score_yt_candidate 評分 → pick_best_music_candidate 挑最佳。

純函數，不碰 yt-dlp 也不碰網路，方便單元測試。
"""
from __future__ import annotations


# 標題出現這些字 → 強烈扣分（非音樂內容）
NON_MUSIC_BLACKLIST = (
    "reaction", "react", "反應",
    "解說", "實況", "直播", "live stream", "livestream",
    "gameplay", "playthrough", "play through", "walkthrough",
    "開箱", "unboxing",
    "podcast", "訪談", "interview",
    "vlog", "talk show",
    "新聞", "news",
    "教學", "tutorial",
    "asmr",
)

# 標題出現這些字 → 加分（音樂內容信號）
MUSIC_HINTS = (
    "mv", "music video", "official",
    "audio", "official audio",
    "cover", "翻唱",
    "完整版", "ost",
    "lyric", "歌詞",
)


def score_yt_candidate(info: dict) -> float:
    """評分一個 YouTube 候選結果。分數越高越像「真正的音樂」。

    評分維度（可互相抵消）：
    - 類別：Music +10；其他類別 -3；無類別 0
    - 黑名單關鍵字：每命中一個 -5
    - 音樂提示關鍵字：每命中一個 +2
    - YouTube Music auto-generated channel（"... - Topic"）：+3
    - 時長：90s-600s +3；60s-900s +1；其他 -2
    """
    score = 0.0
    title    = (info.get("title") or "").lower()
    uploader = (info.get("uploader") or info.get("channel") or "").lower()
    cats     = [c.lower() for c in (info.get("categories") or [])]
    duration = info.get("duration") or 0

    # 類別（非音樂類別罰分需勝過時長加分，避免「歌曲長度的 gameplay BGM」混入）
    if "music" in cats:
        score += 10
    elif cats:
        score -= 6

    # 黑名單
    for kw in NON_MUSIC_BLACKLIST:
        if kw in title:
            score -= 5

    # 音樂提示
    for kw in MUSIC_HINTS:
        if kw in title:
            score += 2

    # YouTube Music 自動產生的 channel（純歌曲，無 MV/反應）
    if uploader.endswith(" - topic"):
        score += 3

    # 時長
    if 90 <= duration <= 600:
        score += 3
    elif 60 <= duration <= 900:
        score += 1
    elif duration > 0:
        score -= 2

    return score


def pick_best_music_candidate(candidates: list[dict]) -> dict | None:
    """從候選清單挑出分數最高的。

    Returns:
        最高分的 candidate dict；候選清單為空時回傳 None。
        即使所有候選分數都 < 0 仍回傳最高分者（fallback：總比沒結果好）。
    """
    if not candidates:
        return None
    scored = [(score_yt_candidate(c), c) for c in candidates]
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]

"""免喚醒詞 task/info 喚醒（helper query）的路由決策 — pure core。

voice_controller._process_queued_query 是 streaming/IO shell；把「這是不是
helper query」「長答案該整段念還是只念通知」這兩個純判斷抽出來，方便單測
（per design_disciplines_for_future_consumers）。
"""
from __future__ import annotations

import random

# helper 答案 ≤ 此長度 → 整段念出；超過 → 只念短通知、完整內容留文字貼文。
HELPER_SPEAK_FULL_MAXLEN = 40

# 長答案的口播通知（Marvin 厭世口吻）。{s} = speaker；內容本身留在貼文不念。
HELPER_NOTIFY_LINES = [
    "{s}，幫你查好了，貼在上面，自己看吧。",
    "{s}，資料找到了，打在頻道上了，看一下。",
    "{s}，查完了，懶得念，上面文字自己讀。",
]


def is_helper_wake(voice_score, dom) -> bool:
    """免喚醒詞的 task/info 喚醒判定。

    沒喊「馬文」→ voice channel 分數低（喊了會 ~1.0，沒喊基線 ~0.3）；
    且喚醒由 task / info 通道帶起（dom 以 task / info 開頭，含 task_search）。
    voice_score=None（無 fusion / 舊路徑）視為非 helper（不改原行為）。
    """
    if voice_score is None or voice_score >= 0.5:
        return False
    if not dom:
        return False
    return dom.startswith("task") or dom.startswith("info")


def helper_speak_plan(
    full_text,
    speaker,
    *,
    full_maxlen: int = HELPER_SPEAK_FULL_MAXLEN,
    notify_lines=HELPER_NOTIFY_LINES,
    rng=random,
):
    """回 (mode, text)。

    short（≤ full_maxlen）→ ("full", 完整答案)：照常整段念。
    long → ("notify", 口播通知)：只念短通知，完整答案另外留貼文（不在此回傳）。
    """
    text = (full_text or "").strip()
    if len(text) <= full_maxlen:
        return ("full", text)
    line = rng.choice(notify_lines).format(s=speaker)
    return ("notify", line)

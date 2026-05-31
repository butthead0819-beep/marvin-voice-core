"""utterance_budget — 把具體環境狀態翻成「話語長度預算」指令注入 LLM prompt（Plan 11 B）。

目標：讓 Marvin 自己看環境決定講多長。fast_awakening 既有依內容分級的字數 budget
（閒聊 50 / 意見 70 / 知識 100 / 活動中 20），但「有沒有在活動」要 LLM 自己猜。這裡把
stream_mode / game_mode / hot_chat 的硬訊號翻成明確指令行，讓那條 budget 由猜測變確定。

純函式：router 組 prompt 時呼叫，回傳要注入的指令行（無特殊環境回 ""）。
"""
from __future__ import annotations

# 越受限的環境字數越少。遊戲最緊（專注遊戲）、音樂次之（背景）、熱聊要快但可稍長。
GAME_BUDGET = 20
STREAM_BUDGET = 30
HOT_CHAT_BUDGET = 35


def environment_directive(*, stream_active: bool = False, game_mode: bool = False, hot_chat: bool = False) -> str:
    """回傳要注入 prompt 的環境長度指令行；多訊號同時取最緊的。無特殊環境回 ""。"""
    if game_mode:
        return f"【環境訊號：玩家正在遊戲中】這算「特定活動中」，嚴格 {GAME_BUDGET} 字以內，不廢話。\n"
    if stream_active:
        return f"【環境訊號：背景正在播放音樂】請格外簡短，{STREAM_BUDGET} 字以內。\n"
    if hot_chat:
        return f"【環境訊號：多人熱聊中】節奏要快，{HOT_CHAT_BUDGET} 字以內。\n"
    return ""

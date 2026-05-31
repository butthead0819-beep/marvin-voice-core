"""utterance_budget — 把具體環境狀態翻成「話語長度預算」指令注入 LLM prompt（Plan 11 B）。

fast_awakening 既有「若玩家正在遊戲/特定活動中：嚴格 20 字」budget，但那要 LLM 自己猜
有沒有在活動。這裡把 stream_mode / game_mode / hot_chat 的硬訊號翻成明確指令，讓既有
budget 由「猜測」變「確定」。純函式：router 在組 prompt 時呼叫。
"""
from __future__ import annotations

from utterance_budget import (
    GAME_BUDGET, HOT_CHAT_BUDGET, STREAM_BUDGET, environment_directive,
)


def test_no_environment_returns_empty():
    assert environment_directive() == ""
    assert environment_directive(stream_active=False, game_mode=False, hot_chat=False) == ""


def test_game_directive_mentions_game_and_budget():
    d = environment_directive(game_mode=True)
    assert "遊戲" in d
    assert str(GAME_BUDGET) in d


def test_stream_directive_mentions_music_and_budget():
    d = environment_directive(stream_active=True)
    assert "音樂" in d
    assert str(STREAM_BUDGET) in d


def test_hot_chat_directive_mentions_hotchat_and_budget():
    d = environment_directive(hot_chat=True)
    assert "熱聊" in d
    assert str(HOT_CHAT_BUDGET) in d


def test_game_takes_priority_over_stream():
    """遊戲最受限，多訊號同時取最緊的 budget。"""
    d = environment_directive(stream_active=True, game_mode=True, hot_chat=True)
    assert "遊戲" in d
    assert "音樂" not in d


def test_stream_takes_priority_over_hot_chat():
    d = environment_directive(stream_active=True, hot_chat=True)
    assert "音樂" in d
    assert "熱聊" not in d


def test_budget_ordering_game_tightest():
    """預算大小：遊戲 ≤ 音樂 ≤ 熱聊（越受限的環境字數越少）。"""
    assert GAME_BUDGET <= STREAM_BUDGET <= HOT_CHAT_BUDGET

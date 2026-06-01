"""
三個現有 SpeakAgent 的 mode_compatible 宣告：

| Agent | mode_compatible | 理由 |
|---|---|---|
| ProactiveTopicAgent | {"normal"} | 主動拋話題；撞 stream/game/radio 都不適合 |
| BridgeAgent | {"normal"} | 轉場提詞；同上 |
| MemoryCallbackAgent | {"normal", "stream"} | 短句 callback；stream 中 hotswap 可發聲 |

絕對防止 silent failure：宣告必須跟 agent 行為一致，CI 跑這份斷言。
"""
from __future__ import annotations

import pytest

from bridge_agent import BridgeAgent
from intent_agents.memory_callback_agent import MemoryCallbackAgent
from proactive_topic_agent import ProactiveTopicAgent


def test_proactive_topic_agent_mode_compatible_normal_only():
    assert ProactiveTopicAgent.mode_compatible == frozenset({"normal"})


def test_bridge_agent_mode_compatible_normal_only():
    assert BridgeAgent.mode_compatible == frozenset({"normal"})


def test_memory_callback_agent_mode_compatible_normal_and_stream():
    """走 vc.speak(proactive=True) → stream 中 hotswap 可發聲（≤30 字）。"""
    assert MemoryCallbackAgent.mode_compatible == frozenset({"normal", "stream"})


# ── 對應的內部 mode 檢查必須移除（避免重複 gate）─────────────────────────────


def test_proactive_topic_agent_no_internal_stream_check():
    """class-attribute 宣告後，agent 不該再有 ad-hoc stream_mode 字串 if 檢查。
    bus 是唯一 gate，agent 重複檢查只會造成日後維護分歧。
    """
    import inspect
    src = inspect.getsource(ProactiveTopicAgent.speak_bid)
    # 沒有任何「if ... stream_mode ...」「if ... radio_mode ...」「if ... current_game ...」
    assert "stream_mode" not in src, "ProactiveTopicAgent 仍有 ad-hoc stream_mode 檢查；bus 已 gate"
    assert "radio_mode" not in src
    assert "current_game" not in src


def test_bridge_agent_no_internal_stream_check():
    import inspect
    src = inspect.getsource(BridgeAgent.speak_bid)
    assert "stream_mode" not in src
    assert "radio_mode" not in src
    assert "current_game" not in src


def test_memory_callback_agent_no_internal_stream_check():
    import inspect
    src = inspect.getsource(MemoryCallbackAgent.speak_bid)
    assert "stream_mode" not in src

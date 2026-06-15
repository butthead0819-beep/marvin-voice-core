"""PlaybackControlAgent skip negative context 修正（2026-05-27 議題 C）.

針對 5/27 分析議題 C：L19「應該下一首就是」、L32「為什麼你下一首」
被 control:skip 0.95 誤判 fast-path。

修法：post_match_filter 過濾 — modal / question word 出現在 skip 關鍵詞之前
→ 視為 chat，拒絕該 schema 命中（落到下個 schema 或 no_match）。

同樣 filter 套用 stop_playback / pause_playback（同類 FP 形狀）。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from intent_bus import IntentContext


def _ctx(query: str, stream_mode: bool = True) -> IntentContext:
    return IntentContext(
        speaker="alice",
        raw_text=query,
        query=query,
        original_raw=query,
        wake_intent=0.9,
        stream_active=stream_mode,
        game_mode=False,
        is_owner=False,
        now=0.0,
        mode="normal",
    )


def _ctrl(stream_mode: bool = True) -> MagicMock:
    ctrl = MagicMock()
    ctrl.stream_mode = stream_mode
    ctrl.play_tts = MagicMock()
    return ctrl


# ── 議題 C 的兩個原 FP case ────────────────────────────────────────────────


def test_l19_modal_prefix_does_not_match_skip():
    """L19「應該下一首就是」應該不命中 skip_track。"""
    from intent_agents.playback_control_agent import PlaybackControlAgent
    agent = PlaybackControlAgent(_ctrl())
    bid = agent.bid(_ctx("應該下一首就是"))
    assert bid.confidence == 0.0, f"expected 0.0, got {bid.confidence} ({bid.reason})"


def test_l32_question_prefix_does_not_match_skip():
    """L32「為什麼你下一首」應該不命中 skip_track。"""
    from intent_agents.playback_control_agent import PlaybackControlAgent
    agent = PlaybackControlAgent(_ctrl())
    bid = agent.bid(_ctx("為什麼你下一首"))
    assert bid.confidence == 0.0


# ── 更多 modal / question chat prefix ─────────────────────────────────────


@pytest.mark.parametrize("query", [
    "應該下一首",
    "可能下一首吧",
    "也許下一首",
    "大概下一首",
    "為什麼下一首",
    "為什麼要切歌",
    "怎麼跳過了",
    "是不是該下一首",
    "有沒有下一首啊",
    "幹嘛換歌",
])
def test_chat_prefixes_reject_skip(query):
    from intent_agents.playback_control_agent import PlaybackControlAgent
    agent = PlaybackControlAgent(_ctrl())
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.0, f"{query!r} should not match skip; got {bid.confidence}"


# ── 同 filter 套用 stop / pause ──────────────────────────────────────────


def test_modal_prefix_rejects_stop():
    from intent_agents.playback_control_agent import PlaybackControlAgent
    agent = PlaybackControlAgent(_ctrl())
    bid = agent.bid(_ctx("應該停止播放了"))
    assert bid.confidence == 0.0


def test_question_prefix_rejects_stop():
    from intent_agents.playback_control_agent import PlaybackControlAgent
    agent = PlaybackControlAgent(_ctrl())
    bid = agent.bid(_ctx("為什麼要停止播放"))
    assert bid.confidence == 0.0


def test_question_prefix_rejects_pause():
    from intent_agents.playback_control_agent import PlaybackControlAgent
    agent = PlaybackControlAgent(_ctrl())
    bid = agent.bid(_ctx("為什麼暫停"))
    assert bid.confidence == 0.0


# ── happy path（不能誤殺）────────────────────────────────────────────────


@pytest.mark.parametrize("query", [
    "下一首",
    "切歌",
    "換歌",
    "跳過",
    "next song",
    "skip",
])
def test_keyword_only_still_matches_skip(query):
    from intent_agents.playback_control_agent import PlaybackControlAgent
    agent = PlaybackControlAgent(_ctrl())
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.85
    assert "skip" in bid.reason.lower()


def test_keyword_with_wake_prefix_still_matches():
    """「馬文下一首」這種 wake + 指令的常見形式必須命中。"""
    from intent_agents.playback_control_agent import PlaybackControlAgent
    agent = PlaybackControlAgent(_ctrl())
    bid = agent.bid(_ctx("馬文下一首"))
    assert bid.confidence == 0.85


def test_chat_marker_after_keyword_still_matches():
    """marker 出現在 keyword 之後不該觸發 filter（filter 只看 prefix）。
    e.g.「下一首為什麼那麼難聽」雖然也是 chat，但 prefix 沒 chat marker →
    本 filter 不負責此 case（後續可由 J2 chat veto 接住）。"""
    from intent_agents.playback_control_agent import PlaybackControlAgent
    agent = PlaybackControlAgent(_ctrl())
    bid = agent.bid(_ctx("下一首為什麼那麼難聽"))
    # 這個 case 暫不擋（避免複雜化 filter）
    assert bid.confidence == 0.85


# ── stream mode gate 仍生效 ───────────────────────────────────────────────


def test_stream_not_active_returns_dense_zero():
    """既有 gate 行為不變。"""
    from intent_agents.playback_control_agent import PlaybackControlAgent
    agent = PlaybackControlAgent(_ctrl(stream_mode=False))
    bid = agent.bid(_ctx("下一首"))
    assert bid.confidence == 0.0
    assert "stream_not_active" in bid.reason


# ── Plan 12 TDD Tests ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_plan12_skip():
    """在 Plan 12 模式下，下一首/切歌指令應清除 mixer 音樂，而不呼叫 vc 的播放停止方法。"""
    from unittest.mock import MagicMock
    from intent_agents.playback_control_agent import PlaybackControlAgent
    
    ctrl = _ctrl()
    ctrl._plan12 = True
    ctrl._mixer = MagicMock()
    
    vc = MagicMock()
    vc.is_connected = MagicMock(return_value=True)
    ctrl.bot = MagicMock()
    ctrl.bot.voice_clients = [vc]
    
    agent = PlaybackControlAgent(ctrl)
    
    bid = agent.bid(_ctx("下一首"))
    await bid.handler()
    
    # 應呼叫 mixer 的清除音樂方法
    ctrl._mixer.clear_music.assert_called_once()
    # vc 的停止方法不應被呼叫，避免 reconnect-loop 抵消
    vc.stop_playing.assert_not_called()
    vc.stop.assert_not_called()


@pytest.mark.asyncio
async def test_execute_plan12_stop():
    """在 Plan 12 模式下，停止播放指令應清除 mixer 且清空佇列，但不呼叫 vc 的停止方法。"""
    from unittest.mock import MagicMock
    from intent_agents.playback_control_agent import PlaybackControlAgent
    
    ctrl = _ctrl()
    ctrl._plan12 = True
    ctrl._mixer = MagicMock()
    ctrl.stream_queue = [{"title": "Song A"}]
    
    vc = MagicMock()
    vc.is_connected = MagicMock(return_value=True)
    ctrl.bot = MagicMock()
    ctrl.bot.voice_clients = [vc]
    
    agent = PlaybackControlAgent(ctrl)
    
    bid = agent.bid(_ctx("停止播放"))
    await bid.handler()
    
    ctrl._mixer.clear_music.assert_called_once()
    assert ctrl.stream_queue == []
    vc.stop.assert_not_called()


@pytest.mark.asyncio
async def test_execute_plan12_pause():
    """在 Plan 12 模式下，暫停指令應設定 mixer 暫停並標記暫停狀態，而不呼叫 vc 的暫停方法。"""
    from unittest.mock import MagicMock
    from intent_agents.playback_control_agent import PlaybackControlAgent
    
    ctrl = _ctrl()
    ctrl._plan12 = True
    ctrl._mixer = MagicMock()
    ctrl.stream_paused = False
    
    vc = MagicMock()
    vc.is_connected = MagicMock(return_value=True)
    ctrl.bot = MagicMock()
    ctrl.bot.voice_clients = [vc]
    
    agent = PlaybackControlAgent(ctrl)
    
    bid = agent.bid(_ctx("暫停"))
    await bid.handler()
    
    ctrl._mixer.set_paused.assert_called_once_with(True)
    assert ctrl.stream_paused is True
    vc.pause.assert_not_called()


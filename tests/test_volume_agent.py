"""VolumeAgent — 語音調音量 intent.

對應 2026-05-27 judge outcomes 分析議題 E：L21「把調小聲一點」是 both-dense-zero
但實際是有效 intent，需要新 agent 接住。

confidence 規約：
  0.95 — 「靜音」「mute」明確指令
  0.90 — 「小聲/大聲」「調低/調高」「音量小/大」「volume up/down」
  0.0  — 無播放（stream/radio 都不在）→ gate "no_playback_active"

mode_compatible = {"normal", "stream"}  # 遊戲模式不該誤觸發

handler 行為：
  - stream_mode → 調 controller.stream_volume（次首生效，對齊既有 UI 按鈕）
  - radio_mode → 調 controller.radio_volume（即時生效，fade loop 觀察）
  - clamp 到 [VOL_MIN, VOL_MAX]
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_bus import IntentContext


pytestmark = pytest.mark.asyncio


def _ctx(query: str, mode: str = "normal") -> IntentContext:
    return IntentContext(
        speaker="alice",
        raw_text=query,
        query=query,
        original_raw=query,
        wake_intent=0.9,
        stream_active=(mode == "stream"),
        game_mode=(mode == "game"),
        is_owner=False,
        now=0.0,
        mode=mode,
    )


def _ctrl(stream_mode=False, radio_mode=False, stream_volume=0.10, radio_volume=0.10):
    ctrl = MagicMock()
    ctrl.stream_mode = stream_mode
    ctrl.radio_mode = radio_mode
    ctrl.stream_volume = stream_volume
    ctrl.radio_volume = radio_volume
    ctrl.VOL_MIN = 0.01
    ctrl.VOL_MAX = 1.00
    ctrl.VOL_STEP = 0.05
    ctrl.play_tts = AsyncMock()
    ctrl.active_text_channel = MagicMock()
    ctrl.active_text_channel.send = AsyncMock()
    return ctrl


# ── mode gate ─────────────────────────────────────────────────────────────


async def test_game_mode_returns_mode_mismatch():
    from intent_agents.volume_agent import VolumeAgent
    agent = VolumeAgent(_ctrl(stream_mode=True))
    bid = agent.bid(_ctx("小聲一點", mode="game"))
    assert bid.confidence == 0.0
    assert "mode_mismatch" in bid.reason


# ── playback gate ─────────────────────────────────────────────────────────


async def test_no_playback_active_dense_zero():
    """stream/radio 都沒開 → 不該 bid（避免「我想小聲一點地講話」誤觸發）。"""
    from intent_agents.volume_agent import VolumeAgent
    agent = VolumeAgent(_ctrl(stream_mode=False, radio_mode=False))
    bid = agent.bid(_ctx("小聲一點"))
    assert bid.confidence == 0.0
    assert "no_playback_active" in bid.reason


# ── volume_down patterns ──────────────────────────────────────────────────


@pytest.mark.parametrize("query", [
    "小聲一點",
    "把調小聲一點",  # 議題 E 的 L21 原案
    "小聲點",
    "調小聲",
    "音量調低",
    "音量小一點",
    "調低的音量",  # 8/18 agent_gaps 修正：particle「的」不該卡掉 adjacency match
    "volume down",
])
async def test_volume_down_patterns(query):
    from intent_agents.volume_agent import VolumeAgent
    agent = VolumeAgent(_ctrl(stream_mode=True))
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.90, f"expected 0.90 for {query!r}, got {bid.confidence}"
    assert "down" in bid.reason or "decrease" in bid.reason or "lower" in bid.reason


# ── volume_up patterns ────────────────────────────────────────────────────


@pytest.mark.parametrize("query", [
    "大聲一點",
    "大聲點",
    "調大聲",
    "音量調高",
    "音量大一點",
    "調高的音量",  # 8/18 agent_gaps 真實樣本「總動調高的音量」的精簡版
    "volume up",
])
async def test_volume_up_patterns(query):
    from intent_agents.volume_agent import VolumeAgent
    agent = VolumeAgent(_ctrl(stream_mode=True))
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.90, f"expected 0.90 for {query!r}, got {bid.confidence}"
    assert "up" in bid.reason or "increase" in bid.reason or "raise" in bid.reason


# ── volume_mute ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("query", ["靜音", "mute"])
async def test_volume_mute_bids_high(query):
    from intent_agents.volume_agent import VolumeAgent
    agent = VolumeAgent(_ctrl(stream_mode=True))
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.95
    assert "mute" in bid.reason


# ── no_match ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("query", [
    "今天天氣不錯",
    "播放周杰倫",
    "下一首",
])
async def test_no_match_returns_dense_zero(query):
    from intent_agents.volume_agent import VolumeAgent
    agent = VolumeAgent(_ctrl(stream_mode=True))
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.0


# ── handler: stream_mode → 調 stream_volume ──────────────────────────────


async def test_handler_volume_down_decreases_stream_volume():
    from intent_agents.volume_agent import VolumeAgent
    ctrl = _ctrl(stream_mode=True, stream_volume=0.50)
    agent = VolumeAgent(ctrl)
    bid = agent.bid(_ctx("小聲一點"))
    await bid.handler()
    # 語音步進 25%：0.50 - 0.25 = 0.25
    assert ctrl.stream_volume == pytest.approx(0.25)


async def test_handler_volume_up_increases_stream_volume():
    from intent_agents.volume_agent import VolumeAgent
    ctrl = _ctrl(stream_mode=True, stream_volume=0.20)
    agent = VolumeAgent(ctrl)
    bid = agent.bid(_ctx("大聲一點"))
    await bid.handler()
    # 0.20 + 0.25 = 0.45
    assert ctrl.stream_volume == pytest.approx(0.45)


async def test_handler_clamps_to_vol_min():
    from intent_agents.volume_agent import VolumeAgent
    ctrl = _ctrl(stream_mode=True, stream_volume=0.05)
    agent = VolumeAgent(ctrl)
    bid = agent.bid(_ctx("小聲一點"))
    await bid.handler()
    # 0.05 - 0.25 = -0.20 → clamp to VOL_MIN=0.01
    assert ctrl.stream_volume == pytest.approx(0.01)


async def test_handler_clamps_to_vol_max():
    from intent_agents.volume_agent import VolumeAgent
    ctrl = _ctrl(stream_mode=True, stream_volume=0.98)
    agent = VolumeAgent(ctrl)
    bid = agent.bid(_ctx("大聲一點"))
    await bid.handler()
    # 0.98 + 0.05 = 1.03 → clamp to VOL_MAX=1.00
    assert ctrl.stream_volume == pytest.approx(1.00)


async def test_handler_mute_sets_to_vol_min():
    from intent_agents.volume_agent import VolumeAgent
    ctrl = _ctrl(stream_mode=True, stream_volume=0.50)
    agent = VolumeAgent(ctrl)
    bid = agent.bid(_ctx("靜音"))
    await bid.handler()
    assert ctrl.stream_volume == pytest.approx(0.01)


# ── handler: radio_mode → 調 radio_volume ────────────────────────────────


async def test_handler_radio_mode_adjusts_radio_volume():
    from intent_agents.volume_agent import VolumeAgent
    ctrl = _ctrl(stream_mode=False, radio_mode=True,
                 stream_volume=0.10, radio_volume=0.20)
    agent = VolumeAgent(ctrl)
    bid = agent.bid(_ctx("大聲一點"))
    await bid.handler()
    assert ctrl.radio_volume == pytest.approx(0.45)
    # stream_volume 不該被動到
    assert ctrl.stream_volume == pytest.approx(0.10)


# ── handler: stream_mode 調音量不依賴熱切換（本地混音台自行輪詢套用） ──────────


async def test_handler_stream_mode_does_not_call_removed_volume_swap(caplog):
    """迴歸測試：controller 是真實形狀（無 request_volume_swap，SimpleNamespace
    而非 MagicMock）時，stream_mode 調音量不該再噴任何熱切換相關錯誤——過去
    ctrl.request_volume_swap() 打在不存在的方法上，AttributeError 被 try/except
    吞掉不會讓測試失敗，但每次調音量都在 log 噴一次假錯誤（2026-09-04 事故，
    只用 MagicMock 當 ctrl 測不出來，因為 MagicMock 對任何屬性都來者不拒）。"""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from intent_agents.volume_agent import VolumeAgent
    ctrl = SimpleNamespace(
        stream_mode=True, radio_mode=False, stream_volume=0.20,
        VOL_MIN=0.01, VOL_MAX=1.00, play_tts=AsyncMock(),
    )
    agent = VolumeAgent(ctrl)
    bid = agent.bid(_ctx("小聲一點"))
    with caplog.at_level(logging.ERROR):
        await bid.handler()
    assert ctrl.stream_volume == pytest.approx(0.01)  # 0.20 - 0.25 被 clamp 到 VOL_MIN
    assert "request_volume_swap" not in caplog.text


async def test_handler_radio_mode_does_not_call_removed_volume_swap(caplog):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from intent_agents.volume_agent import VolumeAgent
    ctrl = SimpleNamespace(
        stream_mode=False, radio_mode=True, radio_volume=0.20,
        VOL_MIN=0.01, VOL_MAX=1.00, play_tts=AsyncMock(),
    )
    agent = VolumeAgent(ctrl)
    bid = agent.bid(_ctx("大聲一點"))
    with caplog.at_level(logging.ERROR):
        await bid.handler()
    assert "request_volume_swap" not in caplog.text


# ── handler: ack ──────────────────────────────────────────────────────────


async def test_handler_plays_ack():
    from intent_agents.volume_agent import VolumeAgent
    ctrl = _ctrl(stream_mode=True)
    agent = VolumeAgent(ctrl)
    bid = agent.bid(_ctx("小聲一點"))
    await bid.handler()
    ctrl.play_tts.assert_called_once()


async def test_handler_sends_text_channel_ack_as_fallback():
    """語音 ack 走 play_tts，會被 [TTS Silence Gate]（使用者仍在講話）靜默吞掉，
    音量調整因此完全沒有回饋、使用者以為指令沒被執行（2026-09-04 實測 case）。
    pause/stop/skip 都有 ch.send() 文字頻道保底，volume 也要比照補上。"""
    from intent_agents.volume_agent import VolumeAgent
    ctrl = _ctrl(stream_mode=True)
    agent = VolumeAgent(ctrl)
    bid = agent.bid(_ctx("小聲一點"))
    await bid.handler()
    ctrl.active_text_channel.send.assert_called_once_with("好，調小")


async def test_handler_text_channel_ack_survives_missing_play_tts():
    """就算 ctrl 沒有 play_tts（或掛掉），文字頻道確認仍要送出。"""
    from intent_agents.volume_agent import VolumeAgent
    ctrl = _ctrl(stream_mode=True)
    ctrl.play_tts = None
    agent = VolumeAgent(ctrl)
    bid = agent.bid(_ctx("大聲一點"))
    await bid.handler()
    ctrl.active_text_channel.send.assert_called_once_with("好，調大")


# ── handler: volume_down 連動全域 TTS Gain ───────────────────────────────


async def test_handler_volume_down_syncs_tts_gain_to_music_volume():
    """「小聲一點」時全域 mixer._tts_gain 直接設成跟音樂音量一樣的值。"""
    from intent_agents.volume_agent import VolumeAgent
    ctrl = _ctrl(stream_mode=True, stream_volume=0.50)
    ctrl._mixer = MagicMock()
    ctrl._mixer._tts_gain = 1.0
    agent = VolumeAgent(ctrl)
    bid = agent.bid(_ctx("小聲一點"))
    await bid.handler()
    # 音樂音量 0.50 → 0.25，TTS gain 直接對齊到 0.25
    assert ctrl._mixer._tts_gain == pytest.approx(0.25)


async def test_handler_volume_up_also_syncs_tts_gain():
    """「大聲一點」也該同步 TTS 音量，不是只有調小才連動。"""
    from intent_agents.volume_agent import VolumeAgent
    ctrl = _ctrl(stream_mode=True, stream_volume=0.20)
    ctrl._mixer = MagicMock()
    ctrl._mixer._tts_gain = 1.0
    agent = VolumeAgent(ctrl)
    bid = agent.bid(_ctx("大聲一點"))
    await bid.handler()
    # 音樂音量 0.20 → 0.45，TTS gain 直接對齊到 0.45
    assert ctrl._mixer._tts_gain == pytest.approx(0.45)


async def test_handler_volume_down_no_mixer_does_not_crash():
    from intent_agents.volume_agent import VolumeAgent
    ctrl = _ctrl(stream_mode=True, stream_volume=0.50)
    ctrl._mixer = None
    agent = VolumeAgent(ctrl)
    bid = agent.bid(_ctx("小聲一點"))
    await bid.handler()  # 不該拋例外
    assert ctrl.stream_volume == pytest.approx(0.25)

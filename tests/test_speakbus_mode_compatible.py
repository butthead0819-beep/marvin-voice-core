"""
SpeakBus mode_compatible — 比照 IntentBus（CLAUDE.md 規範）讓 bus 統一處理
環境 gate，agent 只負責「我有什麼想說」。

絕對防止 silent failure 的三道防線：
  1. register 時強制檢查 agent.mode_compatible 存在（漏宣告 = startup 就 raise）
  2. tick 時 bus 主動過濾 mode_mismatch，agent 自己不再各自 if-stream/radio/game
  3. mode_mismatch 走 outcome log（reason="mode_mismatch:<mode>"），事後可審計

mode 取值：
  - "game"   : voice_controller.game_mode (precedence 最高)
  - "stream" : voice_controller.stream_mode (排除 game)
  - "radio"  : voice_controller.radio_mode (排除 stream/game)
  - "normal" : 都沒有
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from speak_bus import SpeakBid, SpeakBus, SpeakContext


def _mk_ctx(mode: str = "normal") -> SpeakContext:
    return SpeakContext(
        channel_id=1, guild_id=1, silence_seconds=0.0,
        present_speakers=["Alice"], room_mood=None,
        recent_utterances=[], trigger="idle_tick",
        mode=mode,
    )


def _winning_handler():
    async def _h(): pass
    return _h


def _mk_agent(name: str, mode_compatible: frozenset[str] | None, confidence: float = 0.7):
    """工廠：可控 mode_compatible 是否存在（None = 完全沒這屬性）。"""
    agent = MagicMock()
    agent.name = name
    if mode_compatible is not None:
        agent.mode_compatible = mode_compatible
    else:
        # 顯式刪掉 MagicMock 的 auto-attr，模擬「漏宣告」
        try:
            del agent.mode_compatible
        except AttributeError:
            pass
        # MagicMock 對沒設過的屬性會自動生 — 用 spec 限制
        agent = MagicMock(spec=["name", "speak_bid"])
        agent.name = name

    agent.speak_bid = AsyncMock(return_value=SpeakBid(
        agent_name=name, confidence=confidence,
        handler=_winning_handler(), reason="ok",
    ))
    return agent


# ── 1. register 強制檢查 mode_compatible 存在（防 silent failure） ───────────

def test_register_raises_when_agent_missing_mode_compatible():
    """漏宣告 mode_compatible → bus.register 立即 raise，而非 silently 跑壞。"""
    bus = SpeakBus()
    bad_agent = _mk_agent("BadAgent", mode_compatible=None)
    with pytest.raises(TypeError, match="mode_compatible"):
        bus.register(bad_agent)


def test_register_accepts_agent_with_mode_compatible():
    """正確宣告 → register 成功。"""
    bus = SpeakBus()
    good_agent = _mk_agent("GoodAgent", mode_compatible=frozenset({"normal"}))
    bus.register(good_agent)
    assert "GoodAgent" in bus.agents()


def test_register_rejects_empty_mode_compatible():
    """空集合 = agent 在所有模式都不能發話 → 註冊它沒意義，視為錯誤。"""
    bus = SpeakBus()
    empty_agent = _mk_agent("EmptyAgent", mode_compatible=frozenset())
    with pytest.raises(ValueError, match="empty"):
        bus.register(empty_agent)


# ── 2. tick 過濾：mode 不符 → 不收 bid + 寫 outcome dense reason ──────────────

@pytest.mark.asyncio
async def test_tick_filters_agent_when_mode_mismatched():
    """ctx.mode='stream' + agent mode_compatible={"normal"} → 不呼叫 speak_bid。"""
    bus = SpeakBus()
    normal_only = _mk_agent("NormalOnly", mode_compatible=frozenset({"normal"}))
    bus.register(normal_only)

    bid = await bus.tick(_mk_ctx(mode="stream"))

    assert bid is None
    normal_only.speak_bid.assert_not_called()


@pytest.mark.asyncio
async def test_tick_invokes_agent_when_mode_matches():
    """ctx.mode='stream' + agent mode_compatible={'normal','stream'} → 收 bid。"""
    bus = SpeakBus()
    stream_safe = _mk_agent(
        "StreamSafe", mode_compatible=frozenset({"normal", "stream"})
    )
    bus.register(stream_safe)

    bid = await bus.tick(_mk_ctx(mode="stream"))

    assert bid is not None
    assert bid.agent_name == "StreamSafe"
    stream_safe.speak_bid.assert_awaited_once()


@pytest.mark.asyncio
async def test_tick_mixed_filters_only_mismatched():
    """同時 2 agent：mode-match 收 bid，mode-mismatch 跳過。"""
    bus = SpeakBus()
    normal_only = _mk_agent("NormalOnly", mode_compatible=frozenset({"normal"}))
    stream_safe = _mk_agent(
        "StreamSafe", mode_compatible=frozenset({"normal", "stream"}),
        confidence=0.5,
    )
    bus.register(normal_only)
    bus.register(stream_safe)

    bid = await bus.tick(_mk_ctx(mode="stream"))

    assert bid is not None
    assert bid.agent_name == "StreamSafe"
    normal_only.speak_bid.assert_not_called()
    stream_safe.speak_bid.assert_awaited_once()


# ── 3. tick 回傳暴露 mode_mismatch 資訊（給 outcome log 用）──────────────────

@pytest.mark.asyncio
async def test_tick_returns_filtered_agent_names_when_no_winner():
    """所有 agent 都 mode_mismatch → 回 None + 但 bus 內部可查 last_filtered。

    給 voice_controller 寫 outcome 時拉這份清單，把 silent-no-winner 翻成
    visible "no_winner_but_filtered: [agent1, agent2]" log。
    """
    bus = SpeakBus()
    a1 = _mk_agent("A1", mode_compatible=frozenset({"normal"}))
    a2 = _mk_agent("A2", mode_compatible=frozenset({"normal"}))
    bus.register(a1)
    bus.register(a2)

    bid = await bus.tick(_mk_ctx(mode="stream"))

    assert bid is None
    assert set(bus.last_filtered_by_mode()) == {"A1", "A2"}


# ── 4. SpeakContext.mode 必填且為已知值（fail-fast）──────────────────────────

def test_speak_context_mode_required():
    """SpeakContext 必須帶 mode 欄位（dataclass 強制）。"""
    ctx = SpeakContext(
        channel_id=0, guild_id=0, silence_seconds=0.0,
        present_speakers=[], room_mood=None, recent_utterances=[],
        trigger="idle_tick", mode="normal",
    )
    assert ctx.mode == "normal"

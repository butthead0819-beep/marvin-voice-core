"""TDD: ProactiveTopicAgent — 第一個會 bid 的 SpeakAgent。

把 slow_system_loop 內的「靜默 X 秒主動發起話題」這條獨立 timer 觸發路徑遷到
SpeakBus。好處：
  - DuckingAgent 在熱聊期能直接壓制（multiplier=0.2 → effective < MIN_CONFIDENCE）
  - SpeakOutcome log 開始累積真實資料（之前 winner 永遠是 None）
  - 之後新 SpeakAgent 進來時，bus 統一 dispatch 不會撞 TTS

Bid 契約：
  - speak_bid 必須 sync-fast（≤5ms），禁 LLM / I/O / subprocess
  - 不抓在場玩家就不 bid（confidence=0）
  - 撞 stream_mode / radio_mode / game / 太頻繁（last_proactive_time）→ 不 bid
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from proactive_topic_agent import ProactiveTopicAgent
from speak_bus import SpeakContext


def _ctx(silence_seconds: float = 400.0, **overrides) -> SpeakContext:
    base = dict(
        channel_id=100, guild_id=1,
        silence_seconds=silence_seconds,
        present_speakers=["alice", "bob"],
        room_mood=None, recent_utterances=[],
        trigger="idle_tick",
    )
    base.update(overrides)
    return SpeakContext(**base)


def _controller(**overrides):
    """最小 VoiceController stub。"""
    triggered = []
    async def fake_trigger():
        triggered.append(True)
    base = dict(
        proactive_silence_threshold=300.0,
        last_proactive_time=0.0,        # 沒講過話
        radio_mode=False,
        stream_mode=False,
        active_text_channel=SimpleNamespace(id=100, guild=SimpleNamespace(id=1)),
        bot=SimpleNamespace(router=SimpleNamespace(current_game=None)),
        trigger_proactive_topic=fake_trigger,
    )
    base.update(overrides)
    c = SimpleNamespace(**base)
    c._triggered = triggered
    return c


# ── 不該 bid 的情境（dense 0 reasons）─────────────────────────────────────────

@pytest.mark.asyncio
async def test_does_not_bid_when_silence_below_threshold():
    a = ProactiveTopicAgent(_controller())
    bid = await a.speak_bid(_ctx(silence_seconds=100.0))  # < 300
    assert bid is None or bid.confidence == 0.0


@pytest.mark.asyncio
async def test_does_not_bid_when_no_online_members():
    a = ProactiveTopicAgent(_controller())
    bid = await a.speak_bid(_ctx(present_speakers=[]))
    assert bid is None or bid.confidence == 0.0


@pytest.mark.asyncio
async def test_does_not_bid_when_no_active_text_channel():
    a = ProactiveTopicAgent(_controller(active_text_channel=None))
    bid = await a.speak_bid(_ctx())
    assert bid is None or bid.confidence == 0.0


# ── 過期話題不講（2026-06-02）：proactive_topics 由 daily review 維護，
#    review 卡住時 topics 凍結，ProactiveTopicAgent 不該一直翻舊話題浪費 LLM。
#    改讀 suki_memory._meta.review_date，太舊就不 bid（讓冷場 TopicGenerator 即時生）。


def _ctrl_with_review_date(review_date, **kw):
    c = _controller(**kw)
    c.bot.router.memory = SimpleNamespace(get_meta=lambda k, d=None: review_date if k == "review_date" else d)
    return c


@pytest.mark.asyncio
async def test_does_not_bid_when_topics_stale():
    """review_date 太舊（>stale_after_days）→ 不 bid（避免翻舊話題）。"""
    import time as _t
    now = _t.time()
    from datetime import datetime, timedelta
    old = (datetime.fromtimestamp(now) - timedelta(days=10)).strftime("%Y-%m-%d")
    c = _ctrl_with_review_date(old)
    a = ProactiveTopicAgent(c, clock=lambda: now, stale_after_days=3)
    bid = await a.speak_bid(_ctx())
    assert bid is None or bid.confidence == 0.0


@pytest.mark.asyncio
async def test_bids_when_topics_fresh():
    """review_date 是今天 → 正常 bid。"""
    import time as _t
    now = _t.time()
    from datetime import datetime
    today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    c = _ctrl_with_review_date(today)
    a = ProactiveTopicAgent(c, clock=lambda: now, stale_after_days=3)
    bid = await a.speak_bid(_ctx())
    assert bid is not None and bid.confidence > 0


@pytest.mark.asyncio
async def test_fails_open_when_review_date_missing():
    """讀不到 review_date（無 memory / 無 key）→ fail open，正常 bid。"""
    import time as _t
    now = _t.time()
    c = _controller()  # 無 memory.get_meta
    a = ProactiveTopicAgent(c, clock=lambda: now, stale_after_days=3)
    bid = await a.speak_bid(_ctx())
    assert bid is not None and bid.confidence > 0


# ── stream / radio / game mode gates 已 2026-06-01 升到 SpeakBus 層 ──────────
# 透過 ProactiveTopicAgent.mode_compatible = {"normal"} 宣告。覆蓋於：
#   - test_speakbus_agents_mode_compatible.py::test_proactive_topic_agent_mode_compatible_normal_only
#   - test_speakbus_mode_compatible.py::test_tick_filters_agent_when_mode_mismatched
# Agent 本身不再 ad-hoc 檢查 voice mode，避免維護分歧（bus 是唯一 gate）。


@pytest.mark.asyncio
async def test_does_not_bid_when_recently_proactive():
    """1800s 內已主動過 → 不 bid，避免太頻繁。"""
    import time
    c = _controller(last_proactive_time=time.time() - 600)  # 600s 前剛講過
    a = ProactiveTopicAgent(c, min_gap_since_last_s=1800.0)
    bid = await a.speak_bid(_ctx())
    assert bid is None or bid.confidence == 0.0


# ── 該 bid 的情境 ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bids_when_all_conditions_met():
    a = ProactiveTopicAgent(_controller())
    bid = await a.speak_bid(_ctx(silence_seconds=400.0))
    assert bid is not None
    assert bid.confidence > 0.0
    assert bid.agent_name == "ProactiveTopicAgent"


@pytest.mark.asyncio
async def test_bid_confidence_within_reasonable_range():
    """confidence 不要太高（不該蓋過 game/music agent），也不能太低被 MIN_CONFIDENCE 過濾。"""
    a = ProactiveTopicAgent(_controller())
    bid = await a.speak_bid(_ctx(silence_seconds=400.0))
    assert 0.30 <= bid.confidence <= 0.80


@pytest.mark.asyncio
async def test_bid_handler_invokes_trigger_proactive_topic():
    """handler 純薄殼：直接呼叫 controller.trigger_proactive_topic。"""
    c = _controller()
    a = ProactiveTopicAgent(c)
    bid = await a.speak_bid(_ctx(silence_seconds=400.0))
    await bid.handler()
    assert c._triggered == [True]


# ── 不變式：bid 是 sync-fast（不碰 LLM/I/O）────────────────────────────────────

@pytest.mark.asyncio
async def test_bid_does_not_fetch_topics():
    """話題清單 fetch（get_proactive_topics）應在 handler 內，不在 bid 內。"""
    c = _controller()
    # 給一個會 raise 的 router.memory.get_proactive_topics
    def boom():
        raise RuntimeError("bid 不該呼叫這個！")
    c.bot.router.memory = SimpleNamespace(get_proactive_topics=boom)
    a = ProactiveTopicAgent(c)
    # bid 應該安全完成（不碰 get_proactive_topics）
    bid = await a.speak_bid(_ctx(silence_seconds=400.0))
    assert bid is not None  # 沒因 RuntimeError 中斷

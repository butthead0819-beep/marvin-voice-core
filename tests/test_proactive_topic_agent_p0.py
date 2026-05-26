"""ProactiveTopicAgent P0 補強測試（社交圖整合 + 門檻調整）。

P0 目標（per audit）：
  - 把累積 2000+ 筆但無人讀的 SpeakerTopicGraph 接進來，讓 bid reason 有真實上下文
  - 降低 bid cooldown 從 1800s 預設變得更友善（測試用 caller 自己傳值就行）
  - graph 讀取失敗（None / raise）絕對不該破壞 bid（既有路徑仍工作）
"""
from __future__ import annotations

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
    async def fake_trigger():
        pass

    base = dict(
        proactive_silence_threshold=90.0,
        last_proactive_time=0.0,
        active_text_channel=SimpleNamespace(id=100, guild=SimpleNamespace(id=1)),
        radio_mode=False, stream_mode=False,
        trigger_proactive_topic=fake_trigger,
        bot=SimpleNamespace(router=SimpleNamespace(current_game=None)),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _StubGraph:
    """SpeakerTopicGraph stub —回傳 recent(channel_id, n) 結果。"""

    def __init__(self, rows: list[dict] | None = None, *, raise_on_recent: bool = False):
        self._rows = rows or []
        self._raise = raise_on_recent
        self.calls: list[tuple[int, int]] = []

    def recent(self, channel_id: int, n: int = 20) -> list[dict]:
        self.calls.append((channel_id, n))
        if self._raise:
            raise RuntimeError("graph down")
        return self._rows


# ── 1. graph 接入 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bid_reads_graph_when_provided():
    """有 graph → bid 時呼 recent()，把 unique speaker 數 / 樣本句帶進 reason。"""
    g = _StubGraph(rows=[
        {"speaker": "alice", "text": "今天主管又在亂"},
        {"speaker": "bob", "text": "去喝一杯吧"},
        {"speaker": "alice", "text": "好啊"},
    ])
    a = ProactiveTopicAgent(_controller(), topic_graph=g, min_gap_since_last_s=0)
    bid = await a.speak_bid(_ctx(silence_seconds=120))
    assert bid is not None
    assert len(g.calls) >= 1
    # reason 應該帶 graph 訊號（speaker 數或文字摘要）
    assert "graph:" in bid.reason or "speakers:" in bid.reason


@pytest.mark.asyncio
async def test_bid_unchanged_without_graph():
    """無 graph → 原 reason 格式不變（向後相容）。"""
    a = ProactiveTopicAgent(_controller(), topic_graph=None, min_gap_since_last_s=0)
    bid = await a.speak_bid(_ctx(silence_seconds=120))
    assert bid is not None
    assert bid.reason.startswith("social_gap:")


@pytest.mark.asyncio
async def test_graph_read_failure_does_not_break_bid():
    """graph.recent() raise → bid 仍然工作（reason 退原格式）。"""
    g = _StubGraph(raise_on_recent=True)
    a = ProactiveTopicAgent(_controller(), topic_graph=g, min_gap_since_last_s=0)
    bid = await a.speak_bid(_ctx(silence_seconds=120))
    assert bid is not None
    # graph 壞了 → reason 退到 social_gap 字串，不該帶 graph 資訊
    assert bid.reason.startswith("social_gap:")


@pytest.mark.asyncio
async def test_graph_empty_does_not_break_bid():
    """graph 是空的（首次部屬 / 新 channel）也不該爆。"""
    g = _StubGraph(rows=[])
    a = ProactiveTopicAgent(_controller(), topic_graph=g, min_gap_since_last_s=0)
    bid = await a.speak_bid(_ctx(silence_seconds=120))
    assert bid is not None


# ── 2. cooldown 變短 ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cooldown_default_lowered_to_10min():
    """預設 min_gap_since_last_s 應該已經從 1800s 降到 600s (10 min)。"""
    a = ProactiveTopicAgent(_controller())
    assert a._min_gap == 600.0

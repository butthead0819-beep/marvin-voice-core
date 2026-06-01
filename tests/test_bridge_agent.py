"""BridgeAgent — P2 / Week 4 主菜：cross-person 橋接。

A 剛講完 → 從 SpeakerTopicGraph 找在場其他人 B 過去講過相似話題的句子
→ 用 setup 句型把 A 和 B 連起來，讓他們互相聊。

不變式（per docs/social_catalyst_plan.md）：
  - 句型是 setup，不是 bot 質問
  - timing: post_utterance trigger 才 bid（idle_tick 不發 callback bridge）
  - exclude_speaker = A 本人；候選必須在場且非 cooldown 內
  - bridge 後 mark_bridged 該 transcript → 30 天 cooldown
  - hot_chat 時 yield（讓人類繼續聊，不打斷）
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

_NOW = time.time() - 10.0  # 相對最近 ts，避免被 window_days=30 default 過濾

from bridge_agent import BridgeAgent
from speak_bus import SpeakContext
from speaker_topic_graph import SpeakerTopicGraph


def _ctx(
    *,
    trigger: str = "post_utterance",
    last_speaker: str | None = "alice",
    last_text: str | None = "今天主管又在罵人",
    present_speakers: list[str] | None = None,
    hot_chat: bool = False,
    channel_id: int = 100,
) -> SpeakContext:
    room_mood = SimpleNamespace(hot_chat=hot_chat) if hot_chat else None
    return SpeakContext(
        channel_id=channel_id, guild_id=1,
        silence_seconds=0.0,
        present_speakers=present_speakers if present_speakers is not None else ["alice", "bob"],
        room_mood=room_mood,
        recent_utterances=[],
        trigger=trigger,
        last_speaker=last_speaker,
        last_text=last_text,
    )


@pytest.fixture
def graph() -> SpeakerTopicGraph:
    return SpeakerTopicGraph(db_path=":memory:")


def _controller():
    """Stub controller — BridgeAgent 用 vc.speak()（2026-06-01 改）。"""
    spoken: list[str] = []

    async def fake_speak(text: str, **_kwargs):
        spoken.append(text)

    return SimpleNamespace(
        spoken=spoken,
        speak=fake_speak,
        active_text_channel=SimpleNamespace(id=100, guild=SimpleNamespace(id=1)),
        radio_mode=False, stream_mode=False,
        bot=SimpleNamespace(router=SimpleNamespace(current_game=None)),
    )


def _agent(graph, controller=None, **kw):
    return BridgeAgent(
        controller or _controller(),
        topic_graph=graph,
        **kw,
    )


def _seed(graph, channel_id: int, utts: list[tuple[str, str, float]]):
    for speaker, text, ts in utts:
        graph.record_utterance(speaker, channel_id, text, ts=ts)


# ── gate 條件 ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_does_not_bid_on_idle_tick(graph):
    """idle_tick 不該觸發 bridge（只在 post_utterance 機會窗）。"""
    _seed(graph, 100, [("bob", "我之前也被主管罵過", _NOW)])
    a = _agent(graph)
    bid = await a.speak_bid(_ctx(trigger="idle_tick"))
    assert bid.confidence == 0.0
    assert "trigger" in bid.reason


@pytest.mark.asyncio
async def test_does_not_bid_without_last_utterance(graph):
    a = _agent(graph)
    bid = await a.speak_bid(_ctx(last_text=None))
    assert bid.confidence == 0.0


@pytest.mark.asyncio
async def test_does_not_bid_when_only_one_present(graph):
    """≥2 人在場才有 bridge 對象。"""
    a = _agent(graph)
    bid = await a.speak_bid(_ctx(present_speakers=["alice"]))
    assert bid.confidence == 0.0
    assert "too_few_present" in bid.reason or "present" in bid.reason


@pytest.mark.asyncio
async def test_yields_during_hot_chat(graph):
    """熱聊期間禮讓，不發 bridge。"""
    _seed(graph, 100, [("bob", "我也被主管罵", _NOW)])
    a = _agent(graph)
    bid = await a.speak_bid(_ctx(hot_chat=True))
    assert bid.confidence == 0.0
    assert "hot_chat" in bid.reason


# ── graph 命中邏輯 ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bids_when_other_speaker_has_similar_past_topic(graph):
    """A=alice 講主管 → graph 有 bob 講過主管 → bid > 0."""
    _seed(graph, 100, [("bob", "上週主管也對我亂發脾氣", _NOW)])
    a = _agent(graph, min_overlap=0.1)
    bid = await a.speak_bid(_ctx(last_speaker="alice", last_text="今天主管又在罵人"))
    assert bid.confidence > 0
    assert "bridge" in bid.reason
    assert "bob" in bid.reason


@pytest.mark.asyncio
async def test_does_not_bid_when_no_graph_hit(graph):
    """A 講主管 → 在場其他人沒人講過類似話題 → 不 bid。"""
    _seed(graph, 100, [("bob", "晚餐吃什麼", _NOW)])
    a = _agent(graph, min_overlap=0.6)
    bid = await a.speak_bid(_ctx(last_speaker="alice", last_text="今天主管又在罵人"))
    assert bid.confidence == 0.0


@pytest.mark.asyncio
async def test_excludes_last_speaker_from_candidates(graph):
    """A 講主管 → 過去也是 A 講主管的句子不算（self-callback 不是 cross-person bridge）。"""
    _seed(graph, 100, [("alice", "主管真的很煩", _NOW)])
    a = _agent(graph, min_overlap=0.1)
    bid = await a.speak_bid(_ctx(last_speaker="alice", last_text="今天主管又在罵人"))
    assert bid.confidence == 0.0


@pytest.mark.asyncio
async def test_only_picks_present_speakers(graph):
    """bob 不在場時，他的歷史不算 bridge 候選。"""
    _seed(graph, 100, [("bob", "我也被主管罵", _NOW)])
    a = _agent(graph, min_overlap=0.1)
    bid = await a.speak_bid(
        _ctx(last_speaker="alice", last_text="今天主管又在罵人",
             present_speakers=["alice", "charlie"])
    )
    assert bid.confidence == 0.0


# ── handler 行為 ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handler_calls_speak_with_setup_sentence(graph):
    _seed(graph, 100, [("bob", "上週主管對我大小聲", _NOW)])
    c = _controller()
    a = _agent(graph, c, min_overlap=0.1)
    bid = await a.speak_bid(_ctx(last_speaker="alice", last_text="今天主管又在罵人"))
    assert bid.confidence > 0

    await bid.handler()
    assert len(c.spoken) == 1
    line = c.spoken[0]
    # 必須提到 target (bob) 和 source (alice)
    assert "bob" in line
    assert "alice" in line


@pytest.mark.asyncio
async def test_handler_marks_bridged_to_prevent_reuse(graph):
    _seed(graph, 100, [("bob", "我也被主管罵過", _NOW)])
    c = _controller()
    a = _agent(graph, c, min_overlap=0.1, cooldown_days=30)
    bid = await a.speak_bid(_ctx(last_speaker="alice", last_text="今天主管又在罵人"))
    await bid.handler()

    # 同一句不該再被選為 bridge 候選
    bid2 = await a.speak_bid(_ctx(last_speaker="alice", last_text="今天主管又在罵人"))
    assert bid2.confidence == 0.0


# ── 不變式 ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bid_does_not_call_llm_or_io(graph):
    """sync-fast：bid 期間只該 read graph + return bid（不該打 LLM / 不該 await long ops）。
    本測試簡化驗證：bid 在 5ms 內回（雖然不完全 fair test，但 graph 是 sqlite-memory 很快）。"""
    import time as _time
    _seed(graph, 100, [("bob", "主管罵", _NOW)])
    a = _agent(graph, min_overlap=0.1)
    t0 = _time.monotonic()
    await a.speak_bid(_ctx(last_speaker="alice", last_text="主管問題"))
    elapsed = _time.monotonic() - t0
    assert elapsed < 0.05, f"bid 太慢 ({elapsed*1000:.0f}ms)；應該 sync-fast"


@pytest.mark.asyncio
async def test_dense_zero_reasons_are_distinct(graph):
    """plan 規則：每個 dense 0 的 reason 要 distinct（方便 outcome log 追因）。"""
    a = _agent(graph)
    r1 = (await a.speak_bid(_ctx(trigger="idle_tick"))).reason
    r2 = (await a.speak_bid(_ctx(last_text=None))).reason
    r3 = (await a.speak_bid(_ctx(present_speakers=["alice"]))).reason
    r4 = (await a.speak_bid(_ctx(hot_chat=True))).reason
    assert len({r1, r2, r3, r4}) == 4, f"reasons not distinct: {[r1, r2, r3, r4]}"

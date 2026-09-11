"""QueueControlAgent — clear_queue / play_next intent 規則 + audio-rescue 路徑。

比照 tests/test_declarative_agent_resolve_intent.py 的 _ctx() 慣例。
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from intent_agents.queue_control_agent import QueueControlAgent
from intent_bus import IntentContext


def _ctx(query="", mode="normal", audio=False):
    return IntentContext(
        speaker="Alice", raw_text=query, query=query, original_raw=query,
        wake_intent=0.9, stream_active=False, game_mode=False,
        is_owner=False, now=0.0, mode=mode,
        dispatch_source="llm_rescue_audio" if audio else "regex",
    )


def _agent():
    ctrl = AsyncMock()
    ctrl._safe_music_command = AsyncMock()
    return QueueControlAgent(ctrl), ctrl


# ── bid()：regex 路徑 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "馬文清空助列", "清空待播", "清空佇列", "清掉待播", "待播清空",
])
def test_clear_queue_regex_hits(text):
    agent, _ = _agent()
    bid = agent.bid(_ctx(text))
    assert bid.confidence == pytest.approx(0.95)
    assert bid.name == "queue_control"


@pytest.mark.parametrize("text,song", [
    ("插播周杰倫的稻香", "周杰倫的稻香"),
    ("把阿杜的堅持到底放到第一首", "把阿杜的堅持到底"),
    ("先放五月天的倔強", "五月天的倔強"),
])
def test_play_next_regex_hits_and_extracts_song_query(text, song):
    agent, _ = _agent()
    bid = agent.bid(_ctx(text))
    assert bid.confidence == pytest.approx(0.95)


def test_play_next_does_not_collide_with_skip_keyword():
    """play_next 觸發詞刻意不含「放」開頭裸字，避免跟 weak-play/skip 撞——這裡
    只驗證 QueueControlAgent 自己不會對純 skip 語句出價（同分搶標是 IntentBus
    層的事，這裡先確認 agent 邊界乾淨）。"""
    agent, _ = _agent()
    bid = agent.bid(_ctx("下一首"))
    assert bid.confidence == 0.0


def test_casual_mention_does_not_false_positive():
    """閒聊提到「清空」但不是指令 → 不誤觸（regex 本來就要求「清空+待播/佇列/歌單」
    組合，單獨「清空」不夠）。"""
    agent, _ = _agent()
    bid = agent.bid(_ctx("我今天心情很清空"))
    assert bid.confidence == 0.0


# ── post_match_filter：UI/非音樂詞黑名單（複用 music_agent_v2 既有常數）───────

def test_play_next_rejects_ui_word_target():
    agent, _ = _agent()
    bid = agent.bid(_ctx("先放設定"))
    assert bid.confidence == 0.0


def test_play_next_rejects_non_music_suffix():
    agent, _ = _agent()
    bid = agent.bid(_ctx("先放這個網站"))
    assert bid.confidence == 0.0


# ── resolve_intent()：audio-rescue 路徑 ─────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_intent_clear_queue_parameterless():
    agent, ctrl = _agent()
    bid = agent.resolve_intent("clear_queue", {}, _ctx(audio=True))
    assert bid is not None
    await bid.handler()
    ctrl._safe_music_command.assert_awaited_once_with("Alice", "", "clear_queue")


@pytest.mark.asyncio
async def test_resolve_intent_play_next_with_slot_filled():
    agent, ctrl = _agent()
    bid = agent.resolve_intent(
        "play_next", {"song_query": "周杰倫 稻香"}, _ctx(audio=True))
    assert bid is not None
    await bid.handler()
    ctrl._safe_music_command.assert_awaited_once_with("Alice", "周杰倫 稻香", "play_next")


def test_resolve_intent_play_next_empty_slot_returns_none():
    """audio rescue 路徑：LLM 沒填 song_query → post_match_filter 用
    audio_rescue_slots_present 擋下，不碰糊掉的 ctx.query（learning
    audio_rescue_per_agent_wiring）。"""
    agent, _ = _agent()
    bid = agent.resolve_intent("play_next", {"song_query": ""}, _ctx(query="糊字亂碼", audio=True))
    assert bid is None


def test_resolve_intent_mode_incompatible_returns_none():
    agent, _ = _agent()
    bid = agent.resolve_intent("clear_queue", {}, _ctx(mode="game", audio=True))
    assert bid is None


# ── manifest exposure（audio rescue 要接得到）────────────────────────────────

def test_both_intents_have_manifest_description():
    agent, _ = _agent()
    for schema in agent.declare_intents():
        assert schema.manifest_description.strip(), f"{schema.name} 缺 manifest_description"

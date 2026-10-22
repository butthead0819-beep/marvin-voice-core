"""10/21 方案 B：像問句的沒人接喚醒句跳過 Audio Rescue，直接交給問句開場（3.5-flash）。

背景：沒人出價的喚醒句原本一律先等 IntentBus._maybe_rescue（Audio Rescue，~4s）才輪到
voice_controller 的 try_question_opener（3.5-flash 查證，「略」交回聊天）。
「馬文你知道 Gronede2 這個遊戲嗎」被 rescue 白等 4s 選成 just_chatting。

使用者定案：像問句的（question_opener_query 判定非 None）就跳過 rescue，直接回 None
讓 voice_controller 接手 try_question_opener。兩邊共用同一顆純函式 question_opener_query，
確保「跳過 rescue」跟「opener 會接」的條件永遠一致。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_agents.grounded_qa_agent import question_opener_query, try_question_opener
from intent_bus import Bid, IntentBus, IntentContext

pytestmark = pytest.mark.asyncio


def _ctx(query: str, *, in_wake_session: bool = False, low_confidence_wake: bool = False,
         mode: str = "normal"):
    return IntentContext(
        speaker="Alice", raw_text=query, query=query, original_raw=query,
        wake_intent=0.9, stream_active=False, game_mode=False,
        is_owner=False, now=0.0, mode=mode,
        in_wake_session=in_wake_session, low_confidence_wake=low_confidence_wake,
    )


class _StubAgent:
    """沒人出價——仿 test_fast_followup_routing.py 的 _StubAgent。"""
    name = "noop"

    def bid(self, ctx):
        return Bid(name=self.name, confidence=0.0, handler=AsyncMock(), reason="no_match")


class _StubRescue:
    name = "LLMRescue"

    def __init__(self):
        self.calls: list[IntentContext] = []

    async def synthesize(self, ctx):
        self.calls.append(ctx)
        return None


# --- 1. 像問句 → 跳過 rescue ---

async def test_question_like_query_skips_rescue():
    rescue = _StubRescue()
    bus = IntentBus([_StubAgent()], llm_rescue_agent=rescue)

    winner = await bus.dispatch(_ctx("你知道 Gronede2 這個遊戲嗎"))

    assert winner is None
    assert rescue.calls == []


# --- 2. 非問句 / 被排除 → rescue 仍被呼叫 ---

@pytest.mark.parametrize("query", ["隨便聊聊", "播周杰倫的晴天", "這首歌是什麼"])
async def test_non_question_still_calls_rescue(query):
    rescue = _StubRescue()
    bus = IntentBus([_StubAgent()], llm_rescue_agent=rescue)

    winner = await bus.dispatch(_ctx(query))

    assert winner is None
    assert len(rescue.calls) == 1


# --- 3. 問句但 low_confidence_wake → rescue 仍被呼叫 ---

async def test_question_but_low_confidence_wake_still_calls_rescue():
    rescue = _StubRescue()
    bus = IntentBus([_StubAgent()], llm_rescue_agent=rescue)

    winner = await bus.dispatch(_ctx("你知道 Gronede2 這個遊戲嗎", low_confidence_wake=True))

    assert winner is None
    assert len(rescue.calls) == 1


# --- 4. 問句但 mode 不相容 → rescue 仍被呼叫 ---

async def test_question_but_incompatible_mode_still_calls_rescue():
    rescue = _StubRescue()
    bus = IntentBus([_StubAgent()], llm_rescue_agent=rescue)

    winner = await bus.dispatch(_ctx("你知道 Gronede2 這個遊戲嗎", mode="game"))

    assert winner is None
    assert len(rescue.calls) == 1


# --- 5. 一致性：question_opener_query 的判定要跟 try_question_opener 實際會不會呼叫
#        run_grounded_qa 完全一致，否則會出現「跳過 rescue、opener 又不接」的縫。

@pytest.mark.parametrize("query", [
    "你知道 Gronede2 這個遊戲嗎",
    "臺積電今天收盤股價多少",
    "隨便聊聊",
    "播周杰倫的晴天",
    "嗎",
])
async def test_question_opener_query_consistent_with_try_question_opener(monkeypatch, query):
    import intent_agents.grounded_qa_agent as gqa

    run_mock = AsyncMock(return_value="answered")
    monkeypatch.setattr(gqa, "run_grounded_qa", run_mock)

    ctrl = MagicMock()
    ctrl._wake_session_grounded_sticky.return_value = False

    ctx = _ctx(query)
    would_query = question_opener_query(ctx) is not None

    await try_question_opener(ctrl, ctx)
    did_call = run_mock.await_count == 1

    assert would_query == did_call


# --- nowake（無喚醒詞救援）沒有接問句開場 → 像問句也照跑 rescue，不能丟掉 ---

async def test_nowake_question_still_calls_rescue():
    from dataclasses import replace
    rescue = _StubRescue()
    bus = IntentBus([_StubAgent()], llm_rescue_agent=rescue)

    await bus.dispatch(replace(_ctx("你知道 Gronede2 這個遊戲嗎"), dispatch_source="nowake"))

    assert len(rescue.calls) == 1


# --- rescue 主場：像問句但沾音樂 / 播放控制字 → 照跑 rescue（糊掉的指令要靠它救）---

@pytest.mark.parametrize("query", ["希望下次可以找到好聽的歌", "呃 那個 下一手 什麼 的", "可以大聲一點嗎"])
async def test_question_like_but_rescue_domain_still_calls_rescue(query):
    rescue = _StubRescue()
    bus = IntentBus([_StubAgent()], llm_rescue_agent=rescue)

    await bus.dispatch(_ctx(query))

    assert len(rescue.calls) == 1

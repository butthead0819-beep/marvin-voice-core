"""J2 SmallLLMJudge unit tests — LLM rewriter + J1 reuse, mocked LLM.

J2 設計（rewriter 模式）：
  1. 拿原始 STT 文字餵小 LLM（Groq 8B 等）
  2. LLM 回 (rewritten_text, llm_confidence)
  3. 把 rewritten 餵回 regex_judge → 拿到 J1 風格 Bid（含 handler）
  4. 最終 confidence = min(j1_confidence, llm_confidence)，不過度自信

設計原因：handler binding 全靠 agent.bid() — 不引進 J2 專屬 dispatch 路徑。

llm_call 用 DI 注入，測試傳 fake，零外部依賴。
"""
from __future__ import annotations

import asyncio

import pytest

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_bus import IntentContext
from intent_judges.small_llm_judge import small_llm_judge

pytestmark = pytest.mark.asyncio


class _StubAgent(DeclarativeIntentAgent):
    def __init__(self, name, patterns,
                 mode_compatible=frozenset({"normal"})):
        self.name = name
        self.mode_compatible = mode_compatible
        self._schemas = [
            IntentSchema(f"{name}_intent_{i}", conf, [pat])
            for i, (pat, conf) in enumerate(patterns)
        ]

    def declare_intents(self):
        return self._schemas


def _ctx(query: str = "幫我打開那個影片網站", mode: str = "normal",
         wake_intent: float | None = 0.9, speaker: str = "alice") -> IntentContext:
    return IntentContext(
        speaker=speaker, raw_text=query, query=query, original_raw=query,
        wake_intent=wake_intent, stream_active=False, game_mode=False,
        is_owner=False, now=0.0, mode=mode,
    )


def _fake_llm(rewritten: str, confidence: float = 0.9, delay_ms: int = 0):
    async def _call(ctx):
        if delay_ms:
            await asyncio.sleep(delay_ms / 1000)
        return rewritten, confidence
    return _call


# ── happy path ────────────────────────────────────────────────────────────


async def test_small_llm_judge_returns_bid_when_rewrite_matches_regex():
    music = _StubAgent("music", [("打開.*", 0.95)])
    bid = await small_llm_judge(
        _ctx("幫我打開那個影片網站"), [music],
        llm_call=_fake_llm("打開 YouTube", 0.85),
    )
    assert bid.name == "music"
    assert bid.confidence == 0.85  # capped by LLM (min of 0.95 and 0.85)


async def test_small_llm_judge_returns_winning_handler_for_dispatch():
    music = _StubAgent("music", [("打開.*", 0.95)])
    bid = await small_llm_judge(
        _ctx(), [music], llm_call=_fake_llm("打開 YouTube", 0.9),
    )
    assert callable(bid.handler)


# ── confidence capping ───────────────────────────────────────────────────


async def test_small_llm_judge_caps_confidence_at_llm_when_llm_lower():
    music = _StubAgent("music", [("打開.*", 0.95)])
    bid = await small_llm_judge(
        _ctx(), [music], llm_call=_fake_llm("打開 YouTube", 0.6),
    )
    assert bid.confidence == 0.6


async def test_small_llm_judge_caps_confidence_at_regex_when_regex_lower():
    music = _StubAgent("music", [("打開.*", 0.50)])
    bid = await small_llm_judge(
        _ctx(), [music], llm_call=_fake_llm("打開 YouTube", 0.9),
    )
    assert bid.confidence == 0.50


# ── miss paths ────────────────────────────────────────────────────────────


async def test_small_llm_judge_returns_dense_zero_when_rewrite_misses_regex():
    music = _StubAgent("music", [("播放.*", 0.95)])
    bid = await small_llm_judge(
        _ctx(), [music], llm_call=_fake_llm("今天天氣不錯", 0.9),
    )
    assert bid.confidence == 0.0


async def test_small_llm_judge_returns_dense_zero_on_llm_exception():
    music = _StubAgent("music", [("打開.*", 0.95)])

    async def _broken(ctx):
        raise RuntimeError("groq down")

    bid = await small_llm_judge(_ctx(), [music], llm_call=_broken)
    assert bid.confidence == 0.0


async def test_small_llm_judge_returns_dense_zero_on_llm_timeout():
    music = _StubAgent("music", [("打開.*", 0.95)])
    bid = await small_llm_judge(
        _ctx(), [music],
        llm_call=_fake_llm("打開 YouTube", 0.9, delay_ms=500),
        timeout_s=0.05,
    )
    assert bid.confidence == 0.0


async def test_small_llm_judge_returns_dense_zero_when_llm_zero_confidence():
    music = _StubAgent("music", [("打開.*", 0.95)])
    bid = await small_llm_judge(
        _ctx(), [music], llm_call=_fake_llm("打開 YouTube", 0.0),
    )
    assert bid.confidence == 0.0


async def test_small_llm_judge_returns_dense_zero_when_empty_rewrite():
    music = _StubAgent("music", [("打開.*", 0.95)])
    bid = await small_llm_judge(
        _ctx(), [music], llm_call=_fake_llm("", 0.9),
    )
    assert bid.confidence == 0.0


# ── short-circuit ─────────────────────────────────────────────────────────


async def test_small_llm_judge_skips_llm_on_empty_query():
    called = {"n": 0}

    async def _llm(ctx):
        called["n"] += 1
        return "x", 0.9

    bid = await small_llm_judge(_ctx(""), [], llm_call=_llm)
    assert bid.confidence == 0.0
    assert called["n"] == 0  # 不該呼叫 LLM


# ── ctx 透傳 ──────────────────────────────────────────────────────────────


async def test_small_llm_judge_preserves_ctx_metadata_when_rewriting():
    """rewritten ctx 必須帶原 mode / wake_intent / speaker，不然 agent.gate() 失靈。"""
    captured = {}

    class _Recorder(DeclarativeIntentAgent):
        name = "rec"
        mode_compatible = frozenset({"stream"})

        def declare_intents(self):
            return [IntentSchema("rec_intent", 0.95, ["打開.*"])]

        def gate(self, ctx):
            captured["mode"] = ctx.mode
            captured["wake_intent"] = ctx.wake_intent
            captured["speaker"] = ctx.speaker
            return None

    ctx = _ctx(query="x", mode="stream", wake_intent=0.7, speaker="bob")
    await small_llm_judge(
        ctx, [_Recorder()], llm_call=_fake_llm("打開 X", 0.9),
    )
    assert captured["mode"] == "stream"
    assert captured["wake_intent"] == 0.7
    assert captured["speaker"] == "bob"


# ── telemetry ─────────────────────────────────────────────────────────────


async def test_small_llm_judge_attaches_rewrite_to_reason_for_telemetry():
    music = _StubAgent("music", [("打開.*", 0.95)])
    bid = await small_llm_judge(
        _ctx("幫我打開那個"), [music],
        llm_call=_fake_llm("打開 YouTube", 0.9),
    )
    # log 跟 outcome 分析需要看 original/rewritten 對應
    assert "幫我打開那個" in bid.reason
    assert "打開 YouTube" in bid.reason

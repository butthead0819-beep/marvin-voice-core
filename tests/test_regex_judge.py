"""J1 RegexJudge unit tests — pure sync, no LLM, no I/O.

J1 是 parallel judges race 的第一路：拿 raw STT 文字（沒過 cleaner）直接跑
declarative agents 的 bid()，回最高 confidence 的 Bid。race coordinator 之後用
confidence 閾值決定要不要直接 dispatch（命中即 0.95 → 跳過 J2/J3）。

不接 bus、不碰真 agent class。fake DeclarativeIntentAgent subclass 控制 schema。
"""
from __future__ import annotations

import pytest

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_bus import IntentContext
from intent_judges.regex_judge import regex_judge


def _ctx(query: str = "", mode: str = "normal",
         wake_intent: float | None = 0.9) -> IntentContext:
    return IntentContext(
        speaker="alice",
        raw_text=query,
        query=query,
        original_raw=query,
        wake_intent=wake_intent,
        stream_active=False,
        game_mode=(mode == "game"),
        is_owner=False,
        now=0.0,
        mode=mode,
    )


class _StubAgent(DeclarativeIntentAgent):
    """最小 declarative agent：用 (pattern, confidence) tuple list 宣告 schema。"""

    def __init__(self, name, patterns,
                 mode_compatible=frozenset({"normal"}),
                 gate_reason=None):
        self.name = name
        self.mode_compatible = mode_compatible
        self._schemas = [
            IntentSchema(f"{name}_intent_{i}", conf, [pat])
            for i, (pat, conf) in enumerate(patterns)
        ]
        self._gate_reason = gate_reason

    def declare_intents(self):
        return self._schemas

    def gate(self, ctx):
        return self._gate_reason


# ── happy path ────────────────────────────────────────────────────────────


def test_regex_judge_returns_winning_bid_when_single_agent_matches():
    agent = _StubAgent("music", [("打開.*", 0.95)])
    bid = regex_judge(_ctx("打開 YouTube"), [agent])
    assert bid.confidence == 0.95
    assert bid.name == "music"


def test_regex_judge_picks_highest_confidence_among_matches():
    low = _StubAgent("low", [("打開.*", 0.50)])
    high = _StubAgent("high", [("打開.*", 0.90)])
    bid = regex_judge(_ctx("打開 YouTube"), [low, high])
    assert bid.confidence == 0.90
    assert bid.name == "high"


def test_regex_judge_returns_bid_with_nonempty_reason_for_telemetry():
    agent = _StubAgent("music", [("打開.*", 0.95)])
    bid = regex_judge(_ctx("打開 YouTube"), [agent])
    assert bid.reason  # race coordinator + log 需要看得到 winner reason


# ── miss / dense zero ─────────────────────────────────────────────────────


def test_regex_judge_returns_dense_zero_when_no_agent_matches():
    agent = _StubAgent("music", [("播放.*", 0.95)])
    bid = regex_judge(_ctx("今天天氣不錯"), [agent])
    assert bid.confidence == 0.0
    assert bid.reason  # 必須有 distinct reason，不能空白


def test_regex_judge_returns_dense_zero_when_no_agents_provided():
    bid = regex_judge(_ctx("hello"), [])
    assert bid.confidence == 0.0


def test_regex_judge_empty_query_short_circuits_before_agents():
    # 即使 agent 的 pattern 是 ".+"，empty query 也應該直接 0.0，不該迭代
    agent = _StubAgent("greedy", [(".+", 0.95)])
    bid = regex_judge(_ctx(""), [agent])
    assert bid.confidence == 0.0


def test_regex_judge_whitespace_query_short_circuits():
    agent = _StubAgent("greedy", [(".+", 0.95)])
    bid = regex_judge(_ctx("   \t\n"), [agent])
    assert bid.confidence == 0.0


# ── isolation ─────────────────────────────────────────────────────────────


def test_regex_judge_isolates_agent_exception():
    """一個 agent 在 bid() 內炸不該汙染其他 agent。"""

    class _Boom(DeclarativeIntentAgent):
        name = "boom"
        mode_compatible = frozenset({"normal"})

        def bid(self, ctx):
            raise RuntimeError("agent broke")

    good = _StubAgent("music", [("打開.*", 0.95)])
    bid = regex_judge(_ctx("打開 YouTube"), [_Boom(), good])
    assert bid.confidence == 0.95
    assert bid.name == "music"


# ── gate / mode compatibility ─────────────────────────────────────────────


def test_regex_judge_respects_mode_compatibility():
    """game-only agent 在 normal mode 不該贏 race（bid 自帶 mode gate）。"""
    game_only = _StubAgent("busted99", [("猜.*", 0.95)],
                           mode_compatible=frozenset({"game"}))
    bid = regex_judge(_ctx("猜對了", mode="normal"), [game_only])
    assert bid.confidence == 0.0


def test_regex_judge_respects_subclass_gate():
    """agent.gate() 回 reason 時必須阻擋（low_wake_intent 之類）。"""
    gated = _StubAgent("music", [("打開.*", 0.95)],
                      gate_reason="low_wake_intent")
    bid = regex_judge(_ctx("打開 YouTube"), [gated])
    assert bid.confidence == 0.0


# ── handler exposure ──────────────────────────────────────────────────────


def test_regex_judge_winner_carries_handler_for_dispatch():
    """winner Bid 必須帶 handler，race coordinator 命中後直接 await 它。"""
    agent = _StubAgent("music", [("打開.*", 0.95)])
    bid = regex_judge(_ctx("打開 YouTube"), [agent])
    assert callable(bid.handler)

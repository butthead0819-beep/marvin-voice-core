"""J1 + J2 veto wrapper — 把 chat veto 邏輯封裝成 race 可消費的 Bid.

設計：
  1. 跑 regex_judge → j1_bid
  2. 若 j1_bid confidence < fast_path_threshold OR name 不在 veto_prone_intents
     → 直接回 j1_bid（fast-path 不變，零 LLM 呼叫）
  3. 否則跑 chat_classifier_judge → verdict
  4. verdict.is_chat AND verdict.confidence >= veto_threshold
     → 回降級 Bid（confidence=0.0，race 繼續找 J3 winner）
  5. 否則回 j1_bid（J2 確認是真意圖）

race coordinator 完全不需改。J2 LLM 呼叫由 caller DI 注入。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_bus import IntentContext


pytestmark = pytest.mark.asyncio


class _StubAgent(DeclarativeIntentAgent):
    def __init__(self, name, patterns, mode_compatible=frozenset({"normal"})):
        self.name = name
        self.mode_compatible = mode_compatible
        self._schemas = [
            IntentSchema(f"{name}_intent_{i}", conf, [pat])
            for i, (pat, conf) in enumerate(patterns)
        ]

    def declare_intents(self):
        return self._schemas


def _ctx(query: str) -> IntentContext:
    return IntentContext(
        speaker="alice", raw_text=query, query=query, original_raw=query,
        wake_intent=0.9, stream_active=False, game_mode=False, is_owner=False,
        now=0.0, mode="normal",
    )


# ── short-circuit cases（不該呼叫 LLM）─────────────────────────────────────


async def test_no_classifier_returns_j1_as_is():
    """classifier=None → 維持舊行為，永不 veto。"""
    from intent_judges.j1_with_veto import j1_with_veto
    music = _StubAgent("music", [("下一首", 0.95)])
    bid = await j1_with_veto(_ctx("下一首"), [music], chat_classifier_call=None,
                             veto_prone_intents=frozenset({"music"}))
    assert bid.name == "music"
    assert bid.confidence == 0.95


async def test_low_confidence_j1_skips_veto():
    """J1 confidence < fast_path_threshold → 不檢查 veto（J1 本來就不會 fast-path）。"""
    from intent_judges.j1_with_veto import j1_with_veto
    weak = _StubAgent("music", [("播放", 0.50)])  # 弱 bid
    called = [False]

    async def _llm(raw, intent):
        called[0] = True
        return {"is_chat": True, "confidence": 1.0, "reason": "x"}

    bid = await j1_with_veto(
        _ctx("播放"), [weak], chat_classifier_call=_llm,
        fast_path_threshold=0.85, veto_prone_intents=frozenset({"music"}),
    )
    assert called[0] is False
    assert bid.confidence == 0.50


async def test_non_veto_prone_intent_skips_veto():
    """J1 winner name 不在 veto_prone → 不檢查（省 LLM）。"""
    from intent_judges.j1_with_veto import j1_with_veto
    volume = _StubAgent("volume", [("小聲", 0.90)])
    called = [False]

    async def _llm(raw, intent):
        called[0] = True
        return {"is_chat": True, "confidence": 1.0, "reason": "x"}

    bid = await j1_with_veto(
        _ctx("小聲一點"), [volume], chat_classifier_call=_llm,
        veto_prone_intents=frozenset({"music", "playback_control"}),
    )
    assert called[0] is False
    assert bid.name == "volume"


async def test_no_match_returns_dense_zero():
    """J1 沒命中 → 直接回 dense zero，不檢查 veto。"""
    from intent_judges.j1_with_veto import j1_with_veto
    music = _StubAgent("music", [("播放", 0.95)])
    called = [False]

    async def _llm(raw, intent):
        called[0] = True
        return {"is_chat": True, "confidence": 1.0}

    bid = await j1_with_veto(
        _ctx("今天天氣不錯"), [music], chat_classifier_call=_llm,
        veto_prone_intents=frozenset({"music"}),
    )
    assert called[0] is False
    assert bid.confidence == 0.0


# ── veto-prone + high-confidence J1 → 檢查 veto ───────────────────────────


async def test_j2_confirms_intent_returns_j1():
    """J2 說「不是 chat」→ J1 維持 winner，但 reason 留 j2_ran 足跡（可觀測）。"""
    from intent_judges.j1_with_veto import j1_with_veto
    music = _StubAgent("music", [("下一首", 0.95)])

    async def _llm(raw, intent):
        return {"is_chat": False, "confidence": 0.95, "reason": "strong_keyword"}

    bid = await j1_with_veto(
        _ctx("下一首"), [music], chat_classifier_call=_llm,
        veto_prone_intents=frozenset({"music"}),
    )
    assert bid.name == "music"
    assert bid.confidence == 0.95
    # J2 真的執行過 → reason 帶足跡（含 verdict reason），讓 shadow outcome 可觀測
    assert "j2_ran" in bid.reason
    assert "strong_keyword" in bid.reason
    assert "下一首" in bid.reason or "music_intent_0" in bid.reason  # 原 J1 reason 保留


async def test_j2_vetoes_high_confidence_chat():
    """J2 高信心說 chat → 降級 Bid (confidence=0)，race 會接走給 J3。"""
    from intent_judges.j1_with_veto import j1_with_veto
    music = _StubAgent("music", [("下一首", 0.95)])

    async def _llm(raw, intent):
        return {"is_chat": True, "confidence": 0.90, "reason": "modal:應該"}

    bid = await j1_with_veto(
        _ctx("應該下一首就是"), [music], chat_classifier_call=_llm,
        veto_threshold=0.80, veto_prone_intents=frozenset({"music"}),
    )
    assert bid.confidence == 0.0
    assert "vetoed" in bid.reason
    assert "modal" in bid.reason  # verdict reason 進 bid reason
    assert "下一首" in bid.reason or "music_intent_0" in bid.reason  # 原 J1 reason 也保留


async def test_j2_low_confidence_chat_does_not_veto():
    """J2 信心 < veto_threshold → 不 veto，J1 維持 winner。"""
    from intent_judges.j1_with_veto import j1_with_veto
    music = _StubAgent("music", [("下一首", 0.95)])

    async def _llm(raw, intent):
        return {"is_chat": True, "confidence": 0.60, "reason": "uncertain"}

    bid = await j1_with_veto(
        _ctx("下一首"), [music], chat_classifier_call=_llm,
        veto_threshold=0.80, veto_prone_intents=frozenset({"music"}),
    )
    assert bid.name == "music"
    assert bid.confidence == 0.95


# ── LLM 失敗安全 fallback ─────────────────────────────────────────────────


async def test_llm_exception_keeps_j1():
    """LLM 例外 → J1 維持 winner（不誤殺正向 intent）。"""
    from intent_judges.j1_with_veto import j1_with_veto
    music = _StubAgent("music", [("下一首", 0.95)])

    async def _llm(raw, intent):
        raise RuntimeError("groq down")

    bid = await j1_with_veto(
        _ctx("下一首"), [music], chat_classifier_call=_llm,
        veto_prone_intents=frozenset({"music"}),
    )
    assert bid.name == "music"
    assert bid.confidence == 0.95


async def test_llm_timeout_keeps_j1():
    """LLM timeout → J1 維持 winner。"""
    import asyncio
    from intent_judges.j1_with_veto import j1_with_veto
    music = _StubAgent("music", [("下一首", 0.95)])

    async def _slow(raw, intent):
        await asyncio.sleep(2.0)
        return {"is_chat": True, "confidence": 1.0, "reason": "x"}

    bid = await j1_with_veto(
        _ctx("下一首"), [music], chat_classifier_call=_slow,
        veto_prone_intents=frozenset({"music"}), veto_timeout_s=0.05,
    )
    assert bid.name == "music"
    assert bid.confidence == 0.95


# ── J2 足跡可觀測：靜默失敗（timeout/exception）也要留痕跡 ──────────────────

async def test_j2_timeout_leaves_observable_footprint():
    """timeout 是 fail-safe（不 veto），但 reason 必須留 llm_timeout 足跡，
    否則 shadow 分析無法分辨「J2 健康沒否決」vs「J2 一直 timeout 靜默退化」。"""
    import asyncio
    from intent_judges.j1_with_veto import j1_with_veto
    music = _StubAgent("music", [("下一首", 0.95)])

    async def _slow(raw, intent):
        await asyncio.sleep(2.0)
        return {"is_chat": True, "confidence": 1.0, "reason": "x"}

    bid = await j1_with_veto(
        _ctx("下一首"), [music], chat_classifier_call=_slow,
        veto_prone_intents=frozenset({"music"}), veto_timeout_s=0.05,
    )
    assert "j2_ran" in bid.reason
    assert "llm_timeout" in bid.reason


async def test_j2_exception_leaves_observable_footprint():
    """LLM 例外 fail-safe，但 reason 留 llm_exception 足跡。"""
    from intent_judges.j1_with_veto import j1_with_veto
    music = _StubAgent("music", [("下一首", 0.95)])

    async def _llm(raw, intent):
        raise RuntimeError("groq 404")

    bid = await j1_with_veto(
        _ctx("下一首"), [music], chat_classifier_call=_llm,
        veto_prone_intents=frozenset({"music"}),
    )
    assert "j2_ran" in bid.reason
    assert "llm_exception" in bid.reason


async def test_short_circuit_leaves_no_j2_footprint():
    """short-circuit（J2 沒跑）→ reason 不該有 j2_ran 足跡。"""
    from intent_judges.j1_with_veto import j1_with_veto
    volume = _StubAgent("volume", [("小聲", 0.90)])

    async def _llm(raw, intent):
        return {"is_chat": True, "confidence": 1.0, "reason": "x"}

    bid = await j1_with_veto(
        _ctx("小聲一點"), [volume], chat_classifier_call=_llm,
        veto_prone_intents=frozenset({"music"}),  # volume 不在 → short-circuit
    )
    assert "j2_ran" not in bid.reason


# ── handler 必須被保留（race winner 用得到）─────────────────────────────


async def test_j1_winner_handler_is_preserved():
    """J1 命中時 handler 必須能呼叫到，不能被 wrapper 弄掉。"""
    from intent_judges.j1_with_veto import j1_with_veto
    music = _StubAgent("music", [("下一首", 0.95)])

    bid = await j1_with_veto(
        _ctx("下一首"), [music], chat_classifier_call=None,
        veto_prone_intents=frozenset({"music"}),
    )
    assert callable(bid.handler)


async def test_vetoed_bid_handler_is_noop():
    """Veto 後的降級 Bid 不該觸發原 handler。"""
    from intent_judges.j1_with_veto import j1_with_veto

    handler_called = [False]

    class _SideEffectAgent(DeclarativeIntentAgent):
        name = "music"
        mode_compatible = frozenset({"normal"})

        def declare_intents(self):
            return [IntentSchema("skip", 0.95, [r"下一首"])]

        def make_handler(self, schema, slots, ctx):
            async def _h():
                handler_called[0] = True
            return _h

    async def _llm(raw, intent):
        return {"is_chat": True, "confidence": 0.95, "reason": "x"}

    bid = await j1_with_veto(
        _ctx("應該下一首"), [_SideEffectAgent()], chat_classifier_call=_llm,
        veto_prone_intents=frozenset({"music"}),
    )
    # 即使 bid.handler 被誤呼叫，也不該觸發原 side effect
    await bid.handler()
    assert handler_called[0] is False

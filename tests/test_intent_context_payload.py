"""IntentContext.payload 欄位 — Marmo dual-speak PoC 基礎欄位。

驗證：
  - payload 預設 None，現有 ctx 構造方式（不傳 payload）仍能跑
  - payload 可以接 dict
  - dataclasses.replace 保留 payload 欄位
  - 既有 agent（busted99）在 payload=None / 帶 payload 時 bid 行為不變
    （regression：dual-speak 加新欄位不能打到既有 dispatch 路徑）
"""
from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

from intent_agents.busted99_agent import Busted99Agent
from intent_bus import IntentContext


def _ctx(**kw):
    defaults = dict(
        speaker="player1", raw_text="馬文猜21", query="馬文猜21",
        original_raw="馬文猜21", wake_intent=None, stream_active=False,
        game_mode=True, is_owner=False, now=0.0, mode="game",
    )
    defaults.update(kw)
    return IntentContext(**defaults)


def _fake_busted99_cog():
    cog = MagicMock()
    session = MagicMock()
    state = MagicMock()
    state.name = "GUESSING"
    session.state = state
    cog._session = session
    cog.should_suppress_for_game = MagicMock(return_value=False)
    cog.receive_voice_answer_by_speaker = AsyncMock(return_value=True)
    return cog


def _fake_bot(cog):
    bot = MagicMock()
    bot.cogs.get = MagicMock(side_effect=lambda name: cog if name == "Busted99Cog" else None)
    return bot


# ── Field shape ───────────────────────────────────────────────────────────────

def test_intent_context_payload_defaults_none():
    ctx = _ctx()
    assert ctx.payload is None


def test_intent_context_payload_accepts_dict():
    ctx = _ctx(payload={"text": "hello", "job_id": "abc"})
    assert ctx.payload == {"text": "hello", "job_id": "abc"}


def test_dataclasses_replace_preserves_payload():
    """vector intent re-dispatch (intent_bus._resolve_and_redispatch) 用 replace()
    複製 ctx。新加的 payload 欄位必須在 replace 後保留。"""
    ctx = _ctx(payload={"text": "from marmo"})
    new_ctx = replace(ctx, query="rewritten query")
    assert new_ctx.payload == {"text": "from marmo"}
    assert new_ctx.query == "rewritten query"


# ── Regression：既有 agent 不受新欄位影響 ────────────────────────────────────

def test_busted99_bid_unchanged_with_payload_none():
    """既有 ctx 構造（不傳 payload）→ busted99 仍然正常 bid 0.95。"""
    agent = Busted99Agent(_fake_bot(cog=_fake_busted99_cog()))
    bid = agent.bid(_ctx())  # payload 預設 None
    assert bid.confidence == 0.95
    assert bid.reason == "busted99:guessing"


def test_busted99_bid_unchanged_with_payload_set():
    """ctx 帶 payload（不該被 busted99 看到）→ 仍然 bid 0.95，行為不變。"""
    agent = Busted99Agent(_fake_bot(cog=_fake_busted99_cog()))
    bid = agent.bid(_ctx(payload={"text": "marmo task done", "job_id": "x"}))
    assert bid.confidence == 0.95
    assert bid.reason == "busted99:guessing"

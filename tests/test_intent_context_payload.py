"""IntentContext.payload 欄位 — Marmo dual-speak PoC 基礎欄位。

驗證：
  - payload 預設 None，現有 ctx 構造方式（不傳 payload）仍能跑
  - payload 可以接 dict
  - dataclasses.replace 保留 payload 欄位
"""
from __future__ import annotations

from dataclasses import replace

from intent_bus import IntentContext


def _ctx(**kw):
    defaults = dict(
        speaker="player1", raw_text="馬文猜21", query="馬文猜21",
        original_raw="馬文猜21", wake_intent=None, stream_active=False,
        game_mode=True, is_owner=False, now=0.0, mode="game",
    )
    defaults.update(kw)
    return IntentContext(**defaults)


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

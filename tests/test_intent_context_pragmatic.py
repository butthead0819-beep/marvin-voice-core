"""IntentContext pragmatic-signal extension tests.

設計依據：
LLM rescue 路徑要把「字面意圖」與「真正意圖」分離回傳給 agent，agent handler
自己決定如何消化（例如音樂 agent 收到 negative + current_song target 就扣分）。

這層只測 dataclass 契約：
- 新欄位有合理 default（不破壞既有呼叫點）
- frozen 仍然有效（不可變）
- dataclasses.replace 可用（bus 重投時需要）
- 預設值符合 regex 路徑（dispatch_source="regex"、pragmatic_signal/target=None）
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from intent_bus import IntentContext


def _base_kwargs():
    """所有 required 欄位的 minimal kwargs；新欄位刻意不傳，驗 default。"""
    return dict(
        speaker="alice",
        raw_text="下一首",
        query="下一首",
        original_raw="下一首",
        wake_intent=0.9,
        stream_active=False,
        game_mode=False,
        is_owner=False,
        now=0.0,
    )


def test_pragmatic_fields_default_to_regex_source_and_none_signal():
    """新 ctx 不傳 pragmatic 欄位 → regex 路徑預設，signal/target 為 None。

    這是向後相容契約：所有既有呼叫點不傳新欄位仍能 work。
    """
    ctx = IntentContext(**_base_kwargs())
    assert ctx.dispatch_source == "regex"
    assert ctx.pragmatic_signal is None
    assert ctx.pragmatic_target is None


def test_pragmatic_fields_accept_llm_rescue_payload():
    """LLM rescue 路徑能設定三個欄位完整描述「真正意圖」。"""
    ctx = IntentContext(
        **_base_kwargs(),
        dispatch_source="llm_rescue",
        pragmatic_signal="negative",
        pragmatic_target="current_song",
    )
    assert ctx.dispatch_source == "llm_rescue"
    assert ctx.pragmatic_signal == "negative"
    assert ctx.pragmatic_target == "current_song"


def test_intent_context_remains_frozen_after_extension():
    """frozen=True 不能因新欄位被破壞——agent 在 bid() 內不該能 mutate。"""
    ctx = IntentContext(**_base_kwargs())
    with pytest.raises(FrozenInstanceError):
        ctx.pragmatic_signal = "positive"  # type: ignore[misc]


def test_replace_propagates_pragmatic_fields():
    """bus 重投時用 dataclasses.replace 加料；既有 query/depth replace 仍 work，
    新欄位也能在 replace 時設定。"""
    ctx = IntentContext(**_base_kwargs())
    new_ctx = replace(
        ctx,
        query="下一首",
        depth=ctx.depth + 1,
        dispatch_source="llm_rescue",
        pragmatic_signal="negative",
        pragmatic_target="current_song",
    )
    assert new_ctx.speaker == ctx.speaker  # 既有欄位保留
    assert new_ctx.depth == 1
    assert new_ctx.dispatch_source == "llm_rescue"
    assert new_ctx.pragmatic_signal == "negative"
    assert new_ctx.pragmatic_target == "current_song"


def test_pragmatic_signal_only_accepts_documented_polarity_or_none():
    """signal 文件化為 positive/negative/neutral/None；dataclass 不做 runtime
    enforce（型別註解夠用），但測試固定這份契約給未來 reader 看。"""
    for value in ("positive", "negative", "neutral", None):
        ctx = IntentContext(**_base_kwargs(), pragmatic_signal=value)
        assert ctx.pragmatic_signal == value

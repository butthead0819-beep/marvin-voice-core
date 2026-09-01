"""base.py 的 audio-rescue 三分支共用 primitive。

is_audio_rescue / audio_rescue_slot / audio_rescue_slots_present —— 每個接
audio rescue 的 agent 在 gate / post_match_filter / make_handler 三處用它們，
把樣板集中、避免五份 copy-paste 各自寫錯 fallback。範本 = grounded_qa_agent.py。
"""
from __future__ import annotations

from intent_agents.base import (
    AUDIO_RESCUE_SOURCE,
    audio_rescue_slot,
    audio_rescue_slots_present,
    is_audio_rescue,
)
from intent_bus import IntentContext


def _ctx(query="", dispatch_source="regex"):
    return IntentContext(
        speaker="Alice", raw_text=query, query=query, original_raw=query,
        wake_intent=0.9, stream_active=False, game_mode=False,
        is_owner=False, now=0.0, mode="normal", dispatch_source=dispatch_source,
    )


def test_is_audio_rescue_true_only_for_audio_source():
    assert is_audio_rescue(_ctx(dispatch_source=AUDIO_RESCUE_SOURCE)) is True
    assert is_audio_rescue(_ctx(dispatch_source="regex")) is False
    assert is_audio_rescue(_ctx(dispatch_source="llm_rescue")) is False  # 文字版 rescue 不算


def test_audio_rescue_slot_prefers_slot_over_garbled_query():
    ctx = _ctx(query="泡放 妹妹背 洋娃娃", dispatch_source=AUDIO_RESCUE_SOURCE)
    assert audio_rescue_slot({"song_query": "妹妹背著洋娃娃"}, "song_query", ctx) == "妹妹背著洋娃娃"


def test_audio_rescue_slot_strips_whitespace():
    ctx = _ctx(query="x", dispatch_source=AUDIO_RESCUE_SOURCE)
    assert audio_rescue_slot({"topic": "  珠穆朗瑪峰多高  "}, "topic", ctx) == "珠穆朗瑪峰多高"


def test_audio_rescue_slot_falls_back_to_query_when_slot_missing_or_blank():
    ctx = _ctx(query="剩下的糊字", dispatch_source=AUDIO_RESCUE_SOURCE)
    assert audio_rescue_slot({}, "topic", ctx) == "剩下的糊字"
    assert audio_rescue_slot({"topic": "   "}, "topic", ctx) == "剩下的糊字"
    assert audio_rescue_slot({"topic": None}, "topic", ctx) == "剩下的糊字"


def test_audio_rescue_slots_present_requires_all_named_slots_nonblank():
    assert audio_rescue_slots_present({"a": "x", "b": "y"}, "a", "b") is True
    assert audio_rescue_slots_present({"a": "x", "b": "  "}, "a", "b") is False
    assert audio_rescue_slots_present({"a": "x"}, "a", "b") is False
    assert audio_rescue_slots_present({}, "a") is False


def test_audio_rescue_slots_present_no_names_is_vacuously_true():
    assert audio_rescue_slots_present({}) is True

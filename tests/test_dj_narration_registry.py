"""DJ 口白 mode 註冊表（註冊表第 3 刀）——只測新東西：MODES/choose_mode/apply_side_effects。"""
from __future__ import annotations

import inspect

from dj_narration_orchestrator import (
    DJMaterials,
    MODES,
    NarrationPlan,
    apply_side_effects,
    choose_mode,
    tts_emotion_for,
)
from dj_topic_selector import TopicCooldownStore


def test_apply_side_effects_is_not_a_coroutine_function():
    assert not inspect.iscoroutinefunction(apply_side_effects)


def test_modes_registry_has_exactly_expected_names():
    expected = {
        "revival", "memory_match", "song", "life", "interest",
        "emotional_highlight", "news", "callback", "activity", "guide",
        "conversation", "atmosphere", "quick", "reason",
    }
    assert set(MODES.keys()) == expected


def test_tts_emotion_for_matches_known_table():
    upbeat = {"life", "interest", "activity", "news"}
    calm = {"atmosphere", "emotional_highlight"}
    normal = {"quick", "reason", "song", "conversation", "memory_match", "revival", "guide", "callback"}
    for mode in upbeat:
        assert tts_emotion_for(mode) == "upbeat", mode
    for mode in calm:
        assert tts_emotion_for(mode) == "calm", mode
    for mode in normal:
        assert tts_emotion_for(mode) == "normal", mode
    assert tts_emotion_for("short") == "normal"


def test_revival_takes_priority_over_memory_match_and_does_not_cool_down(tmp_path):
    store = TopicCooldownStore(path=str(tmp_path / "cd.json"))
    materials = DJMaterials(revival_lines=["a"], memory_evidence="證據")
    topic, mode = choose_mode(materials, store)
    assert (topic, mode) == (None, "revival")
    assert store.is_cool("證據"), "revival 命中不該連帶把 memory_evidence 標記冷卻"


def test_apply_side_effects_conversation_partially_consumed(tmp_path):
    entries = [{"speaker": "Bob", "text": "今天好累"}, {"speaker": "Alice", "text": "我也是"}]
    consumed_flags = {"Bob": True, "Alice": False}

    class _Bank:
        def is_consumed(self, entry):
            return consumed_flags[entry["speaker"]]

        def mark_consumed(self, entries, ts):
            self.marked = entries

    bank = _Bank()
    plan = NarrationPlan(mode="conversation", topic=None, ctx_lines=[], tts_emotion="normal",
                          side_effects=["consume_conversation"])
    materials = DJMaterials()
    result = apply_side_effects(
        plan, materials, conv_entries=entries, heat_bank=lambda: bank,
        callback_src={}, consume_callback=lambda who, item: None,
    )
    assert result.mode == "conversation"
    assert result.ctx_lines == [
        "【你熟悉他的生活】頻道近期對話：\nAlice：「我也是」",
        "串場方向：用剛才頻道對話的氣氛自然接過去就好，不用硬掰新話題。",
    ]
    assert bank.marked == [{"speaker": "Alice", "text": "我也是"}]


def test_apply_side_effects_callback_consumes_and_swallows_exception():
    plan = NarrationPlan(mode="callback", topic="X", ctx_lines=[], tts_emotion="normal",
                          side_effects=["consume_callback"])
    materials = DJMaterials()
    item = {"text": "買叉子"}

    calls = []
    result = apply_side_effects(
        plan, materials, conv_entries=[], heat_bank=lambda: None,
        callback_src={"X": ("Alice", item)},
        consume_callback=lambda who, it: calls.append((who, it)),
    )
    assert calls == [("Alice", item)]
    assert result.mode == "callback"

    def _boom(who, it):
        raise RuntimeError("boom")

    result2 = apply_side_effects(
        plan, materials, conv_entries=[], heat_bank=lambda: None,
        callback_src={"X": ("Alice", item)},
        consume_callback=_boom,
    )
    assert result2.mode == "callback"


def test_reason_overrides_atmosphere_and_last_fallback_keeps_gacha_pick(tmp_path):
    """池裡只剩 atmosphere/quick（quick 只墊底）→ 抽中 atmosphere；有 autopilot 理由就蓋成 reason，
    last_fallback 記的仍是扭蛋抽中的 atmosphere。"""
    store = TopicCooldownStore(path=str(tmp_path / "cd.json"))
    topic, mode = choose_mode(DJMaterials(autopilot_reason="這首是 Alice 點過的歌"), store)
    assert (topic, mode) == (None, "reason")
    assert store.get_last_fallback() == "atmosphere"

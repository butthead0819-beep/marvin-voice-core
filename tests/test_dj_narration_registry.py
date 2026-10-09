"""DJ 口白 mode 註冊表（註冊表第 3 刀）——只測新東西：MODES/choose_mode/apply_side_effects。"""
from __future__ import annotations

import inspect

from dj_narration_orchestrator import (
    DJMaterials,
    MODES,
    NarrationPlan,
    apply_side_effects,
    choose_mode,
    plan_narration,
    tts_emotion_for,
)
from dj_topic_selector import TopicCooldownStore
from hook_collision import Collision


def test_apply_side_effects_is_not_a_coroutine_function():
    assert not inspect.iscoroutinefunction(apply_side_effects)


def test_modes_registry_has_exactly_expected_names():
    expected = {
        "revival", "collision", "memory_match", "song", "life", "interest",
        "news", "callback", "activity", "guide",
        "conversation", "atmosphere", "quick", "reason",
    }
    assert set(MODES.keys()) == expected


def test_tts_emotion_for_matches_known_table():
    upbeat = {"life", "interest", "activity", "news"}
    calm = {"atmosphere"}
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


def _coll() -> Collision:
    return Collision(speaker="showay", chat_quote="我覺得他真的拿走了什麼東西",
                     lyric_line="拿走了什麼", matched_key="拿走了什麼", score=5, kind="literal")


def test_collision_forced_wins_over_gacha(tmp_path):
    m = DJMaterials(collision=_coll(), life=["生活素材"], has_conversation=True)
    plan = plan_narration(m, TopicCooldownStore(path=str(tmp_path / "cd.json")))
    assert plan.mode == "collision"


def test_collision_loses_to_revival(tmp_path):
    m = DJMaterials(collision=_coll(), revival_lines=["剛剛大家聊過的話"])
    plan = plan_narration(m, TopicCooldownStore(path=str(tmp_path / "cd.json")))
    assert plan.mode == "revival"


def test_collision_beats_memory_match(tmp_path):
    m = DJMaterials(collision=_coll(), memory_evidence="showay 說過喜歡這首")
    plan = plan_narration(m, TopicCooldownStore(path=str(tmp_path / "cd.json")))
    assert plan.mode == "collision"


def test_collision_ctx_has_both_quotes_verbatim(tmp_path):
    m = DJMaterials(collision=_coll())
    plan = plan_narration(m, TopicCooldownStore(path=str(tmp_path / "cd.json")))
    joined = "\n".join(plan.ctx_lines)
    assert "我覺得他真的拿走了什麼東西" in joined
    assert "拿走了什麼" in joined
    assert "showay" in joined


def test_collision_does_not_touch_topic_cooldown(tmp_path):
    """collision 是 forced 層，命中就直接回傳、不進 select_mode 的扭蛋池——
    若真的被 mark_used/set_last_fallback 寫到，get_last_fallback() 會被改成非 None。"""
    store = TopicCooldownStore(path=str(tmp_path / "cd.json"))
    plan = plan_narration(DJMaterials(collision=_coll()), store)
    assert plan.mode == "collision"
    assert store.get_last_fallback() is None


def test_no_collision_means_no_collision_mode(tmp_path):
    plan = plan_narration(DJMaterials(), TopicCooldownStore(path=str(tmp_path / "cd.json")))
    assert plan.mode != "collision"

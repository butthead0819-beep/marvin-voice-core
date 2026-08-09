"""每日人格排班——星期幾輪替，summon 前同步套用，guard 防同日重複套用。

設計：personas/schedule.yaml 用 monday~sunday 對照 character+mood；套用結果寫回
suki_dna.json 的 character/persona_tag，並蓋 schedule_applied_date 當 guard（比照
[[daily_review 的 once/day guard]]，但用 dna 欄位而非額外檔案）。
"""
import persona_loader
from cogs.voice_controller_connection import ConnectionMixin
from personality_config import apply_schedule_entry

_applied_today = ConnectionMixin._persona_schedule_applied_today


def test_not_applied_when_field_absent():
    assert _applied_today({}, "2026-08-09") is False


def test_not_applied_when_date_mismatch():
    dna = {"schedule_applied_date": "2026-08-08"}
    assert _applied_today(dna, "2026-08-09") is False


def test_applied_when_date_matches():
    dna = {"schedule_applied_date": "2026-08-09"}
    assert _applied_today(dna, "2026-08-09") is True


def test_apply_schedule_entry_sets_character_mood_and_date():
    state = apply_schedule_entry({}, "marmo", "刀子嘴豆腐心", "2026-08-09")

    assert state["character"] == "marmo"
    assert state["persona_tag"] == "刀子嘴豆腐心"
    assert state["schedule_applied_date"] == "2026-08-09"


def test_apply_schedule_entry_preserves_current_game():
    state = apply_schedule_entry(
        {"current_game": "Apex Legends"}, "deadpan_operator", "邏輯關機", "2026-08-09"
    )

    assert state["current_game"] == "Apex Legends"


def test_schedule_yaml_has_all_seven_weekdays():
    schedule = persona_loader.load_schedule()

    for key in ConnectionMixin._SCHEDULE_WEEKDAY_KEYS:
        assert key in schedule, f"schedule.yaml 缺 {key}"
        assert "character" in schedule[key]
        assert "mood" in schedule[key]

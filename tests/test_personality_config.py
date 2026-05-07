from personality_config import (
    adjust_axis,
    apply_character_preset,
    build_personality_prompt_context,
    normalize_personality_state,
)
from marvin_prompts import PromptManager


def test_normalize_personality_state_adds_axes_and_legacy_fields():
    state = normalize_personality_state({"toxicity": 5, "current_game": "none"})

    assert state["character"] == "marvin"
    assert state["toxicity"] == 5
    assert state["current_game"] == "none"
    assert set(state["axes"]) >= {"oppression", "resignation", "compassion"}


def test_adjust_axis_clamps_value():
    state = normalize_personality_state({})

    state = adjust_axis(state, "compassion", 2.0)
    assert state["axes"]["compassion"] == 1.0

    state = adjust_axis(state, "compassion", -5.0)
    assert state["axes"]["compassion"] == 0.0


def test_apply_character_preset_preserves_current_game():
    state = apply_character_preset({"current_game": "Apex Legends"}, "deadpan_operator")

    assert state["character"] == "deadpan_operator"
    assert state["current_game"] == "Apex Legends"
    assert state["axes"]["directness"] >= 0.9


def test_prompt_manager_injects_unified_personality_context():
    prompt = PromptManager().get_instruction(
        "fast_awakening",
        dna=normalize_personality_state({"axes": {"oppression": 0.5, "resignation": 0.3, "compassion": 0.2}}),
    )

    assert "統一人格參數" in prompt
    assert "壓抑=0.50" in prompt
    assert "無奈=0.30" in prompt
    assert "同情=0.20" in prompt

from personality_config import (
    build_personality_prompt_context,
    normalize_personality_state,
)
from marvin_prompts import PromptManager


def test_normalize_personality_state_adds_axes_and_legacy_fields():
    state = normalize_personality_state({"toxicity": 5, "current_game": "none"})

    assert state["character"] == "marvin"
    assert "toxicity" not in state
    assert state["current_game"] == "none"
    assert set(state["axes"]) >= {"oppression", "resignation", "compassion"}


def test_normalize_personality_state_forces_persona_tag_to_preset_and_strips_stale_fields():
    state = normalize_personality_state({
        "persona_tag": "宇宙之泪的詩聖",
        "toxicity": 1,
        "schedule_applied_date": "2026-10-08",
    })

    assert state["persona_tag"] == "厭世機器人馬文"
    assert "toxicity" not in state
    assert "schedule_applied_date" not in state


def test_apply_character_preset_switches_prompt_context():
    marvin_state = normalize_personality_state({"character": "marvin"})
    marmo_state = normalize_personality_state({"character": "marmo"})

    marvin_prompt = build_personality_prompt_context(marvin_state)
    marmo_prompt = build_personality_prompt_context(marmo_state)

    assert "馬文" in marvin_prompt and "行星般大腦" in marvin_prompt
    assert "馬末" in marmo_prompt and "刀子嘴豆腐心" in marmo_prompt
    assert marvin_prompt != marmo_prompt


def test_prompt_manager_injects_unified_personality_context():
    prompt = PromptManager().get_instruction(
        "fast_awakening",
        dna=normalize_personality_state({"axes": {"oppression": 0.5, "resignation": 0.3, "compassion": 0.2}}),
    )

    assert "統一人格參數" in prompt
    assert "壓抑=0.50" in prompt
    assert "無奈=0.30" in prompt
    assert "同情=0.20" in prompt

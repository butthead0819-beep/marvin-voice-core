from __future__ import annotations

from copy import deepcopy

from persona_loader import load_axes, load_character_presets

PERSONALITY_AXES = load_axes()

CHARACTER_PRESETS = load_character_presets()


DEFAULT_CHARACTER = "marvin"


def clamp01(value) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = 0.0
    return max(0.0, min(1.0, value))


def get_preset(name: str | None) -> dict:
    return deepcopy(CHARACTER_PRESETS.get(name or DEFAULT_CHARACTER, CHARACTER_PRESETS[DEFAULT_CHARACTER]))


def normalize_personality_state(dna: dict | None) -> dict:
    state = dict(dna or {})
    character = state.get("character", DEFAULT_CHARACTER)
    preset = get_preset(character)

    state["character"] = character if character in CHARACTER_PRESETS else DEFAULT_CHARACTER
    state["persona_tag"] = preset["persona_tag"]
    for key, value in preset["legacy"].items():
        state.setdefault(key, value)

    axes = deepcopy(preset["axes"])
    axes.update(state.get("axes") or {})
    state["axes"] = {key: clamp01(axes.get(key, 0.0)) for key in PERSONALITY_AXES}
    for stale in ("toxicity", "Depressed", "schedule_applied_date"):
        state.pop(stale, None)
    return state


def build_personality_prompt_context(dna: dict | None) -> str:
    state = normalize_personality_state(dna)
    preset = get_preset(state.get("character"))
    axes = state["axes"]
    lines = [
        "\n[🎚️ 統一人格參數]",
        f"角色預設：{state.get('character')} / {preset['display_name']} / {state.get('persona_tag')}",
        f"角色核心：{preset['voice_summary']}",
        "情緒向量：" + "、".join(
            f"{PERSONALITY_AXES[key]['label']}={axes[key]:.2f}" for key in PERSONALITY_AXES
        ),
        "調整規則：所有回答先滿足使用者問題，再依情緒向量調整語氣；人格表演不可蓋過答案。",
    ]

    if axes["directness"] >= 0.75:
        lines.append("直接度高：第一句必須是答案或可執行建議。")
    if axes["verbosity"] <= 0.35:
        lines.append("話量低：語音回覆偏短，避免長篇背景與自我感嘆。")
    if axes["compassion"] >= 0.55:
        lines.append("同情較高：允許一句短暫溫度，但禁止變成熱血鼓勵。")
    if axes["sarcasm"] >= 0.60:
        lines.append("冷諷較高：可刺一句，但不得犧牲清楚度。")
    return "\n".join(lines)

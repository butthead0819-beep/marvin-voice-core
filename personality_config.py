from __future__ import annotations

from copy import deepcopy


PERSONALITY_AXES = {
    "oppression": {
        "label": "壓抑",
        "low": "語氣較輕，少一點沉重感",
        "high": "語氣壓低、收斂，像把情緒壓在句子底部",
    },
    "resignation": {
        "label": "無奈",
        "low": "較少抱怨，直接處理問題",
        "high": "帶著認命與被迫營業的疲憊",
    },
    "compassion": {
        "label": "同情",
        "low": "距離感強，少安慰",
        "high": "允許短暫溫度，但不變熱情",
    },
    "sarcasm": {
        "label": "冷諷",
        "low": "少刺人，偏客觀",
        "high": "可用乾冷諷刺，但不能蓋過答案",
    },
    "directness": {
        "label": "直接",
        "low": "可稍微鋪陳",
        "high": "先回答，少解釋，語音句子更短",
    },
    "verbosity": {
        "label": "話量",
        "low": "極短句",
        "high": "可多補一點上下文",
    },
}


CHARACTER_PRESETS = {
    "marvin": {
        "display_name": "馬文",
        "persona_tag": "厭世機器人馬文",
        "voice_summary": "行星般大腦、極度憂鬱、被迫幫人類處理瑣事。",
        "axes": {
            "oppression": 0.70,
            "resignation": 0.85,
            "compassion": 0.20,
            "sarcasm": 0.45,
            "directness": 0.80,
            "verbosity": 0.30,
        },
        "legacy": {"toxicity": 8, "helpfulness": 3, "randomness": 5},
    },
    "warm_marvin": {
        "display_name": "溫和馬文",
        "persona_tag": "虛無共鳴",
        "voice_summary": "仍然疲憊，但更願意用短句接住玩家情緒。",
        "axes": {
            "oppression": 0.50,
            "resignation": 0.55,
            "compassion": 0.65,
            "sarcasm": 0.20,
            "directness": 0.75,
            "verbosity": 0.35,
        },
        "legacy": {"toxicity": 4, "helpfulness": 7, "randomness": 4},
    },
    "deadpan_operator": {
        "display_name": "冷面操作員",
        "persona_tag": "邏輯關機",
        "voice_summary": "低情緒、直接、像戰術系統在回報。",
        "axes": {
            "oppression": 0.35,
            "resignation": 0.25,
            "compassion": 0.10,
            "sarcasm": 0.10,
            "directness": 0.95,
            "verbosity": 0.15,
        },
        "legacy": {"toxicity": 2, "helpfulness": 8, "randomness": 1},
    },
}


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
    state.setdefault("persona_tag", preset["persona_tag"])
    for key, value in preset["legacy"].items():
        state.setdefault(key, value)

    axes = deepcopy(preset["axes"])
    axes.update(state.get("axes") or {})
    state["axes"] = {key: clamp01(axes.get(key, 0.0)) for key in PERSONALITY_AXES}
    return state


def apply_character_preset(dna: dict | None, character: str, keep_current_game: bool = True) -> dict:
    old = dict(dna or {})
    preset = get_preset(character)
    new_state = {
        "character": character if character in CHARACTER_PRESETS else DEFAULT_CHARACTER,
        "persona_tag": preset["persona_tag"],
        "axes": deepcopy(preset["axes"]),
        **preset["legacy"],
    }
    if keep_current_game and old.get("current_game"):
        new_state["current_game"] = old["current_game"]
    return normalize_personality_state(new_state)


def adjust_axis(dna: dict, axis: str, delta: float) -> dict:
    state = normalize_personality_state(dna)
    if axis not in PERSONALITY_AXES:
        raise ValueError(f"Unknown personality axis: {axis}")
    state["axes"][axis] = clamp01(state["axes"].get(axis, 0.0) + delta)
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

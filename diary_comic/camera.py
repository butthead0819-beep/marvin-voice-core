"""攝影分鏡：每格指定鏡頭角度，讓整頁有張力起伏（不再每格平視中景）。

- 第一格：建立鏡頭（wide establishing）→ 交代場景。
- 英雄格（punchline）：戲劇性低角度仰拍 → 全頁的張力高點。
- 中段：在多種鏡頭間輪替（特寫/俯視/過肩/傾斜/景深），避免單調。
"""
from __future__ import annotations

# 中段鏡頭池（輪替用）—— 刻意混角度與景別製造節奏
_SHOTS = (
    "tight close-up on the characters' faces, shallow depth of field, emotional",
    "high angle looking down on the whole group",
    "over-the-shoulder shot, strong foreground framing",
    "dynamic dutch tilt, energetic and off-kilter",
    "wide shot with strong foreground-to-background depth",
    "side profile two-shot, cinematic framing",
    "extreme close-up on one character's eyes/mouth, intense and intimate",
    "backlit silhouette shot, dramatic rim light, mood",
    "low angle hero shot with the character bursting toward the viewer",
)

_ESTABLISH = "wide establishing shot, slightly low angle, sets the whole scene"
_HERO = ("dramatic low angle looking up at the key character, dynamic cinematic composition, "
         "the character large and powerful BURSTING out of the frame toward the viewer, "
         "high tension, broken-border energy")


def shot_for(index: int, total: int, is_hero: bool) -> str:
    """回傳該格的鏡頭指示字串。英雄格最戲劇、第一格建立、其餘輪替。"""
    if is_hero:
        return _HERO
    if index == 0:
        return _ESTABLISH
    return _SHOTS[(index - 1) % len(_SHOTS)]

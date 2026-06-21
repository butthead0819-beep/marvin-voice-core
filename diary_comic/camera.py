"""攝影分鏡：每格指定鏡頭，走「遠景→中景→特寫」三距離節奏，避免每格證件照。

節奏（Jack 2026-06-21）：
- 遠景 Wide：交代環境/空間（每頁第一格）。
- 中景 Medium：角色動作、肢體語言、互動。
- 特寫 Close-up：放大眼神/嘴角/關鍵道具，傳達強烈情緒。
三者交替（不連三同距），英雄格用特寫推情緒高潮。
"""
from __future__ import annotations

_WIDE = (
    "wide establishing shot showing the whole room and environment, sense of space",
    "wide shot with strong foreground-to-background depth, sense of place",
)
_MEDIUM = (
    "medium shot showing the characters' actions and body language",
    "medium two-shot, characters interacting, gestures and posture visible",
    "over-the-shoulder medium shot, foreground framing",
    "backlit silhouette shot, dramatic rim light, mood",
)
_CLOSEUP = (
    "close-up on a character's face — eyes and mouth, strong emotion",
    "extreme close-up on the eyes or a key prop, intense and intimate",
    "close-up reaction shot, exaggerated expression",
)

# 給舊測試 / 相容用的全鏡頭池（shot_for 實際用下方節奏）
_SHOTS = _WIDE + _MEDIUM + _CLOSEUP

_ESTABLISH = _WIDE[0]
_HERO = ("dramatic close-up on the key character's face, low angle, intense emotion, "
         "exaggerated reaction bursting out of the frame, high tension, broken-border energy")

# 距離節奏：第一格遠景後，中↔特來回跳、定期回遠景 re-establish（不連三同距）
_RHYTHM = ("wide", "medium", "closeup", "medium", "closeup")
_POOLS = {"wide": _WIDE, "medium": _MEDIUM, "closeup": _CLOSEUP}


def shot_for(index: int, total: int, is_hero: bool) -> str:
    """回傳該格鏡頭。英雄格=情緒特寫；第一格=遠景；其餘走遠/中/特節奏。"""
    if is_hero:
        return _HERO
    if index == 0:
        return _ESTABLISH
    kind = _RHYTHM[index % len(_RHYTHM)]
    pool = _POOLS[kind]
    return pool[index % len(pool)]

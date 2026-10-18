"""多人在場時回覆前面叫名字，讓大家知道 Marvin 在回誰（2026-10-17 使用者需求）。純函式、零 IO。"""
from __future__ import annotations


def address_prefix(speaker: str, online_members: list[str], text: str) -> str:
    """真人 ≥2 且 text 沒以 speaker 開頭 → f"{speaker}，"，否則 ""。"""
    if not speaker:
        return ""
    if len(online_members or []) < 2:
        return ""
    if (text or "").strip().lower().startswith(speaker.lower()):
        return ""
    return f"{speaker}，"

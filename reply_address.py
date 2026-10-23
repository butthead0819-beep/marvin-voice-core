"""多人在場時回覆前面叫名字，讓大家知道 Marvin 在回誰（2026-10-17 使用者需求）。純函式、零 IO。"""
from __future__ import annotations


def address_prefix(speaker: str, online_members: list[str], text: str) -> str:
    """真人 ≥2 且 text 沒以 speaker 開頭 → f"{speaker}，"，否則 ""。

    text 以 speaker 開頭時還要檢查名字後面那個字元：若是 ASCII 英數字（例如
    "Also" 誤判成 "Al" 開頭），代表那不是名字本身，仍視為沒叫過名字（10/23 review）。
    """
    if not speaker:
        return ""
    if len(online_members or []) < 2:
        return ""
    stripped = (text or "").strip()
    if stripped.lower().startswith(speaker.lower()):
        ch = stripped[len(speaker):len(speaker) + 1]
        if not (ch and ch.isascii() and ch.isalnum()):
            return ""
    return f"{speaker}，"

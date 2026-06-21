"""Character bible：speaker → 固定動物 + 一致外型描述。

跨格/跨天角色一致性的地基。先用文字描述（餵 prompt 保持外型一致）；之後可加一欄
reference image 路徑做更強的一致性。新人沒在冊上 → fallback 通用動物。

動物對應沿用先前測試圖建立的設定，保持連載感。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Character:
    animal: str
    appearance: str  # 一致的外型描述，每次出圖都餵同一句
    ref_image: str | None = None  # 之後可放定裝參考圖路徑（更強一致性）


CHARACTER_BIBLE: dict[str, Character] = {
    "狗與露": Character("dog", "a scruffy friendly light-brown dog"),
    "狗與鹿": Character("dog", "a scruffy friendly light-brown dog"),  # 同人 STT 變體
    "showay": Character("owl", "a wise owl with round glasses"),
    "陳進文": Character("beaver", "a hard-working beaver in a carpenter apron"),
    "大肚": Character("cat", "a round chubby orange cat with a big belly"),
    "weakgogo": Character("penguin", "a small round penguin"),
}

# 馬文：不是說話者卡司，是角落旁白機器人（出圖時固定加在角落）
MARVIN = Character("robot", "a small round robot DJ with a glowing screen face, world-weary")

FALLBACK = Character("duck", "a generic plain duck (a passerby)")


def get_character(speaker: str) -> Character:
    return CHARACTER_BIBLE.get(speaker, FALLBACK)


def describe(speaker: str) -> str:
    """回該說話者對應動物的一致外型描述；未知 → fallback 鴨。"""
    return get_character(speaker).appearance


def cast_description(speakers: list[str]) -> str:
    """把一格的說話者全換成動物描述，用「; 」串起來。空 → 空字串。"""
    return "; ".join(describe(s) for s in speakers)

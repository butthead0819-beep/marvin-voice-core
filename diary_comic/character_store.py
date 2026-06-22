"""Character bible：speaker → 固定動物 + 一致外型描述。

跨格/跨天角色一致性的地基。先用文字描述（餵 prompt 保持外型一致）；之後可加一欄
reference image 路徑做更強的一致性。新人沒在冊上 → fallback 通用動物。

動物對應沿用先前測試圖建立的設定，保持連載感。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

_DNA_DIR = "records"  # 每日更新的 speech_dna 詳細檔目錄（cwd 相對；LIVE 跑時=最新統計）


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


def _recent_topics(speaker: str) -> str:
    """每日更新的 stress_topics（近期愛聊什麼）→ 清成「tech、work、drinking」。無檔→空。"""
    try:
        with open(os.path.join(_DNA_DIR, f"speech_dna_{speaker}.json"), encoding="utf-8") as f:
            raw = json.load(f).get("stress_topics", "") or ""
        topics = [t.split("（")[0].strip() for t in re.split(r"[、,]", raw) if t.strip()]
        return "、".join(topics[:4])
    except Exception:
        return ""


def persona(speaker: str) -> dict:
    """該 speaker 的人設。

    語音 voice（style_summary/catchphrases/quirks）= BUILTIN 手寫最大化版（靜態）。
    recent_topics = 每日更新的近期興趣（stress_topics）—— 這欄會隨每日分析持續變。
    """
    dna = {}
    try:
        from impression_engine import get_speech_dna
        dna = get_speech_dna(speaker) or {}
    except Exception:
        pass
    return {"style_summary": dna.get("style_summary", ""),
            "catchphrases": list(dna.get("catchphrases", []))[:5],
            "quirks": list(dna.get("quirks", []))[:4],
            "emotional_style": dna.get("emotional_style", ""),
            "recent_topics": _recent_topics(speaker)}


def cast_quirks(speakers: list[str]) -> str:
    """一格說話者的表情/姿勢提示（餵出圖 prompt）：每人 emotional_style。沒人設→空字串。"""
    bits = []
    for s in speakers:
        emo = persona(s)["emotional_style"]
        if emo:
            bits.append(f"{s}：{emo}")
    return "；".join(bits)


def persona_brief(speaker: str) -> str:
    """一行人設 brief（給故事導演/分鏡 prompt）：動物 + 說話風格 + 口頭禪。"""
    ch = get_character(speaker)
    p = persona(speaker)
    bits = [f"{speaker}（{ch.animal}：{ch.appearance}）"]
    if p["style_summary"]:
        bits.append(p["style_summary"])
    if p["catchphrases"]:
        bits.append("口頭禪：" + "、".join(p["catchphrases"]))
    if p["recent_topics"]:
        bits.append("近期愛聊：" + p["recent_topics"])
    return "｜".join(bits)

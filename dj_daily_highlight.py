"""每日亮點（highlight_of_the_day）→ DJ 生活素材（純函式，無 I/O）。

9/30 使用者定：每日回顧寫的整段生活敘述本來完全沒被 DJ 用到（漏斗第2步）——
老朋友熟悉你的生活，理應拿來當串場素材。但醫療健康類的句子不當素材：
在大家面前講「你還在醫院」很尷尬，這類內容不該被公開複誦。

主角是否在場的過濾交給呼叫端（dj_topic_selector.select_mode 用 LifeCore.speakers
判斷），這裡只負責從 highlight 文字切句、濾醫療句、组成 LifeCore。
"""
from __future__ import annotations

import re

from dj_life_context import LifeCore

MEDICAL_KEYWORDS = ("醫院", "住院", "生病", "看醫生", "看病", "開刀", "手術", "發燒", "感冒",
                    "吃藥", "診所", "急診", "病房", "打針", "受傷", "身體不舒服")


def highlight_life_cores(member: str, highlight, *, max_len: int = 40) -> list[LifeCore]:
    """member 的 highlight_of_the_day 文字 → LifeCore 列表。

    highlight 是前一次每日回顧寫的整段敘述，隔天再講會是「今天」變成錯誤時態，
    因此句中的「今天」一律替換成「最近」。
    """
    if not member or not isinstance(highlight, str) or not highlight.strip():
        return []

    out: list[LifeCore] = []
    for raw in re.split(r"[。！？!?；;\n]", highlight):
        s = raw.strip()
        if len(s) < 6:
            continue
        if any(kw in s for kw in MEDICAL_KEYWORDS):
            continue
        s = s.replace("今天", "最近")[:max_len]
        out.append(LifeCore(text=f"{member}：{s}", meme_id="", speakers=(member,)))
    return out

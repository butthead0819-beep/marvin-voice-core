"""DJ 扭蛋式動機組裝器（Gacha Narrator ✕ 3 種世俗說話動機）。

拒絕 AI 假文青與塑膠扮演濾鏡，維持 Marvin 唯一放鬆/毒舌老友本色。
依據素材可用性，在 3 種真實說話動機中隨機切換：
1. irony: 抓矛盾吐槽（Spot the Irony）——歌詞刺點 ✕ 現實生活反差
2. tea: 爆世俗小八卦（Spill the Tea）——幕後製作軼事 ✕ 世俗真實八卦
3. hook: 丟聽覺懸念（Drop the Hook）——音軌彩蛋 ✕ 耳朵聽覺勾引
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Collection


@dataclass(frozen=True)
class GachaMotivation:
    mode: str
    instruction: str


def pick_gacha_motivation(
    song_card: dict[str, Any] | None,
    topic: str = "",
    forced_mode: str | None = None,
    exclude: Collection[str] = (),
) -> GachaMotivation | None:
    """從歌曲卡素材與上下文場景中，扭蛋抽出一段給 LLM 的說話動機指引。
    
    若歌曲卡為空或沒有有效素材，回傳 None（安全回退至一般串場）。
    exclude：這首歌最近用過的動機（非 forced_mode 時從候選去掉；去完沒有回 None）。
    """
    if not song_card or not isinstance(song_card, dict):
        return None

    guide = (song_card.get("audiophile_guide") or "").strip()
    lyric = song_card.get("lyric_hook")

    candidates: list[str] = []
    if lyric and isinstance(lyric, dict) and lyric.get("quote"):
        candidates.append("irony")
    if guide and guide != "無":
        candidates.append("hook")
        candidates.append("tea")

    if not candidates:
        return None

    chosen_mode = None
    if forced_mode:
        if forced_mode in candidates:
            chosen_mode = forced_mode
        else:
            return None
    else:
        candidates = [c for c in candidates if c not in exclude]
        if not candidates:
            return None
        chosen_mode = random.choice(candidates)

    if chosen_mode == "irony":
        quote = lyric["quote"]
        instruction = (
            f"串場動機【抓矛盾吐槽】：歌詞裡這句「{quote}」刺中要害。"
            "把它拿來跟現場氣氛或生活反差做個幽默吐槽，別講大道理，一句話直戳痛點。"
        )
    elif chosen_mode == "hook":
        instruction = (
            "串場動機【丟聽覺懸念】：從導聆素材裡挑出一個具體的樂器、唱腔或聲場細節，"
            "像勾引大家耳朵一樣提醒聽眾等下注意聽，不用把整段導聆背出來。"
        )
    elif chosen_mode == "tea":
        instruction = (
            "串場動機【爆世俗小八卦】：像跟熟朋友爆料一樣，聊聊這首歌幕後的真實小八卦或創作軼事，"
            "口氣自然放鬆，嚴禁套用八股反轉句型。"
        )
    else:
        return None

    return GachaMotivation(mode=chosen_mode, instruction=instruction)

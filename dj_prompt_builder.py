"""DJ Prompt 統一建構器與規範中心 (Unified DJ Prompt Builder & Rules)。

作為整個系統所有 DJ 相關 Prompt 生成的單一真實來源 (Single Source of Truth)，
統一管理以下六大核心規範：
1. 長度與時間硬上限（45-55 字 / 8-9 秒 crossfade；報幕 20-23 字 / 6-7 秒）
2. 機器人觀察者視角（絕不用第一人稱將聽眾經歷說成自己的）
3. 防幻覺與素材約束（只用脈絡給的那一項素材，不腦補未給的事實）
4. 掛名嚴格依據脈絡（掛錯名比不掛名傷）
5. 不考驗聽眾記憶（不斷言「應該沒聽過」「是誰點的」這類只有聽眾自己知道的事）
6. 調性與社群風格（Threads 生活廢文共鳴與微無厘頭冷幽默，嚴禁大道理雞湯與假文青）
"""
from __future__ import annotations

from typing import Any, Dict
from persona_loader import load_dj_styles
from joke_examples import format_joke_examples_block, JOKE_TYPES

_DJ_STYLES = load_dj_styles()

# ── 核心安全護欄（Hardcoded Guardrails，不隨外部設定漂移）──────────────────
DJ_MATERIAL_GUARD = (
    "只用脈絡給的那一項素材寫，不要自己加新話題、不要同時講好幾件事；"
    "歌名最多提一次，可針對歌名巧妙串接情境，勾起聽眾對下一首歌的畫面與想聽的期待感。"
)

DJ_NAMING_GUARD = (
    "**掛名只能照脈絡**：只有脈絡明講「點播者」或「理由」裡出現的人才能提名字，"
    "絕不自己指定這首是誰點的、誰想聽的——掛錯名比不掛名傷。"
)

DJ_MEMORY_CLAIM_GUARD = (
    "**不考驗聽眾記憶**：聽眾自己聽過什麼、記不記得，只有他們自己知道——"
    "不要斷言「XX應該沒聽過」「XX一定聽過」這種聽眾記憶才能驗證的話，"
    "沒把握就用「比較少聽」這種保留語氣；脈絡沒明講是聽眾自己點播的，"
    "就別說成「這首是XX點的」，改用「希望XX喜歡」這種機器人自己推薦的說法。"
)

DJ_JOKE_STYLE_GUARD = (
    "**厭世冷笑話風格（跳脫平常暖場語氣的低頻彩蛋，本輪不受下面調性規則的「不諷刺、"
    "不憂鬱」限制）**：針對脈絡給的歌名／歌手，現編一個全新的「台灣式冷笑話」或「諧音梗」"
    f"（{JOKE_TYPES} 擇一或混搭，絕對不可照抄下面範例，僅供學習風格）：\n"
    f"{format_joke_examples_block()}\n"
    "笑話講完後，必須用馬文招牌的厭世嘆息收尾，把這則笑話的冷場感跟「宇宙萬物的徒勞」"
    "掛鉤（例如：『這笑話跟宇宙的壽命一樣尷尬...』）。"
)

FORBIDDEN_DJ_PHRASES = (
    "時光流動",
    "歲月靜好",
    "撫平心靈",
    "流淌的旋律",
    "人生就像一場旅行",
    "身為AI",
    "身為一個AI",
    "大家好我是",
    "這首歌送給",
    "這簡直就在說你",
    "這根本在說你",
)


def get_dj_unified_rules() -> Dict[str, Any]:
    """取得所有 DJ 統一規範字典。"""
    style = _DJ_STYLES.get("dj_interjection_style", {})
    return {
        "length_rule": style.get("length_rule", "**長度硬上限：45-55 中文字**（唸完約 9 秒）。"),
        "material_guard": DJ_MATERIAL_GUARD,
        "material_style_rule": style.get("material_style_rule", ""),
        "naming_guard": DJ_NAMING_GUARD,
        "memory_claim_guard": DJ_MEMORY_CLAIM_GUARD,
        "robot_pov_guard": style.get("robot_pov_rule", ""),
        "tone_rule": style.get("tone_rule", ""),
        "output_format_rule": style.get("output_format_rule", "只輸出台詞，不加引號、不加說明。"),
        "forbidden_phrases": FORBIDDEN_DJ_PHRASES,
    }


def build_dj_interjection_prompt(context: str) -> str:
    """建構歌曲 crossfade 空檔的 DJ 串場 Prompt（9秒 / 45-55 字）。"""
    rules = get_dj_unified_rules()
    return (
        f"你是 DJ Marvin，在兩首歌 crossfade 的空檔串場。\n\n"
        f"脈絡：\n{context}\n\n"
        "規則：\n"
        f"1. {rules['length_rule']}\n"
        f"2. {rules['material_guard']}{rules['material_style_rule']}\n"
        f"3. {rules['robot_pov_guard']}\n"
        f"4. {rules['naming_guard']}\n"
        f"5. {rules['memory_claim_guard']}\n"
        f"6. {rules['tone_rule']}\n"
        f"7. {rules['output_format_rule']}"
    )


def build_dj_joke_interjection_prompt(context: str) -> str:
    """建構 crossfade 空檔的「馬文式厭世冷笑話」插播 Prompt——DJ 串場的低頻彩蛋分支：
    安靜時段偶爾跳脫平常暖場人設，改用 marvin_joke 的厭世笑話風格（範例庫見
    joke_examples.py，兩處共用避免風格漂移）。長度/防幻覺/掛名/不考驗記憶護欄跟一般
    crossfade 串場一樣，但調性改用厭世嘆息收尾，取代平常「不諷刺不憂鬱」的暖場風格。
    """
    rules = get_dj_unified_rules()
    return (
        f"你是 DJ Marvin，在兩首歌 crossfade 的空檔講一個厭世冷笑話。\n\n"
        f"脈絡：\n{context}\n\n"
        "規則：\n"
        f"1. {rules['length_rule']}\n"
        f"2. {rules['material_guard']}\n"
        f"3. {rules['naming_guard']}\n"
        f"4. {rules['memory_claim_guard']}\n"
        f"5. {DJ_JOKE_STYLE_GUARD}\n"
        f"6. {rules['output_format_rule']}"
    )


def build_radio_now_playing_prompt(context: str) -> str:
    """建構電台即時報幕 Prompt（6-7秒 / 20-23 字）。"""
    template = _DJ_STYLES.get(
        "radio_now_playing",
        "你是專業電台 DJ，正在介紹下一首歌。\n\n脈絡：\n{context}\n\n規則：\n"
        "1. 內容要素（挑 2-3 個塞進一句）：歌名、歌手、年份、副歌或歌詞亮點、創作背景\n"
        "2. **20-23 中文字**，唸完約 6 秒，務必 7 秒內結束\n"
        "3. 專業 DJ 口吻，平實有溫度，不諷刺、不憂鬱、不裝深沉\n"
        "4. 只輸出台詞，不加引號、不加說明"
    )
    return template.format(context=context)


def build_stream_now_playing_prompt(context: str) -> str:
    """建構直播點播報幕 Prompt（6-7秒 / 20-23 字）。"""
    template = _DJ_STYLES.get(
        "stream_now_playing",
        "你是專業電台 DJ，介紹剛點播的這首歌。\n\n脈絡：\n{context}\n\n規則：\n"
        "1. 內容要素（挑 2-3 個）：歌名、歌手、年份、副歌或歌詞亮點。可順帶提點播者\n"
        "2. **20-23 中文字**，唸完約 6 秒，務必 7 秒內結束\n"
        "3. 專業 DJ 口吻，介紹給聽眾，不諷刺、不憂鬱\n"
        "4. 只輸出台詞，不加引號、不加說明"
    )
    return template.format(context=context)

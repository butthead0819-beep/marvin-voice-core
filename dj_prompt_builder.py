"""DJ Prompt 統一建構器與規範中心 (Unified DJ Prompt Builder & Rules)。

作為整個系統所有 DJ 相關 Prompt 生成的單一真實來源 (Single Source of Truth)，
統一管理以下五大核心規範：
1. 長度與時間硬上限（45-55 字 / 8-9 秒 crossfade；報幕 20-23 字 / 6-7 秒）
2. 機器人觀察者視角（絕不用第一人稱將聽眾經歷說成自己的）
3. 防幻覺與素材約束（只用脈絡給的那一項素材，不腦補未給的事實）
4. 掛名嚴格依據脈絡（掛錯名比不掛名傷）
5. 調性與社群風格（Threads 生活廢文共鳴與微無厘頭冷幽默，嚴禁大道理雞湯與假文青）
"""
from __future__ import annotations

from typing import Any, Dict
from persona_loader import load_dj_styles

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
        f"5. {rules['tone_rule']}\n"
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

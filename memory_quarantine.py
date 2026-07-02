"""防線③ 記憶寫入 sanity 檢疫閘 — LLM 產出進持久層前的驗證。

Tier 3 失效模式：**錯誤記憶會複利**——LLM 掰的時戳（suki_memory 曾被
污染到 2024，bot 2026-03 才誕生）、非答案（「未知」）、垃圾長文一旦
merge 進 player record，之後每次 prompt 注入都在放大錯誤。

掛點：gemini_router_content.extract_memory 的 LLM JSON →
update_player_memory 之間。剔除項全數 log（shadow 可觀察誤殺率）。
kill-switch：env MARVIN_MEMORY_QUARANTINE=0（預設開）。
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

BOT_ORIGIN_YEAR = 2026          # Simon 2026-03 誕生；更早的「互動歷史」＝LLM 掰的
MAX_VALUE_LEN = 80              # 記憶勾點應短；更長＝LLM 在倒垃圾

# LLM 非答案模式（抽不到資訊時的填充語，不是資料）
_NON_ANSWERS = re.compile(
    r"^(未知|不明|無|無法確定|不確定|沒有提到|未提及|不清楚|N/?A\.?|n/?a|null|none|unknown)$",
    re.IGNORECASE,
)

# 「互動歷史」型 key：值裡出現 bot 誕生前的年份＝掰時戳。
# 出生年/紀念日等本來就該是過去年份的 key 不在此列。
_INTERACTION_KEY = re.compile(r"met|first|認識|初次|上次|相遇|加入|見面", re.IGNORECASE)
_YEAR = re.compile(r"(19\d{2}|20\d{2})")


def _bad_value(key: str, v) -> str | None:
    """回剔除原因；None＝通過。"""
    if v is None:
        return f"{key}: 空值"
    s = str(v).strip()
    if not s:
        return f"{key}: 空字串"
    if _NON_ANSWERS.match(s):
        return f"{key}: LLM 非答案「{s}」"
    if len(s) > MAX_VALUE_LEN:
        return f"{key}: 超長 {len(s)} 字"
    if _INTERACTION_KEY.search(key):
        for y in _YEAR.findall(s):
            if int(y) < BOT_ORIGIN_YEAR:
                return f"{key}: 互動歷史掰時戳「{s}」（bot {BOT_ORIGIN_YEAR} 才誕生）"
    return None


def quarantine(extracted, speaker: str) -> tuple[dict, list[str]]:
    """驗證 LLM 抽取的記憶 → (可寫入的乾淨資料, 剔除原因清單)。

    保守剔除：只擋確定是垃圾的（非答案/超長/掰時戳），可疑但可能真的
    （如出生年 1990）一律放行——誤殺真記憶比放進垃圾更傷。
    """
    rejected: list[str] = []
    if not isinstance(extracted, dict):
        return {}, [f"整包非 dict（type={type(extracted).__name__}），全數剔除"]

    clean: dict = {}
    for section, val in extracted.items():
        if section == "personal_info" and isinstance(val, dict):
            kept = {}
            for k, v in val.items():
                reason = _bad_value(str(k), v)
                if reason is None:
                    kept[k] = v
                else:
                    rejected.append(reason)
            if kept:
                clean["personal_info"] = kept
        elif section in ("taboos", "likes", "dislikes") and isinstance(val, list):
            kept_list = [x for x in val if isinstance(x, str) and x.strip()]
            dropped = len(val) - len(kept_list)
            if dropped:
                rejected.append(f"{section}: 剔除 {dropped} 個非字串/空項")
            if kept_list:
                clean[section] = kept_list
        else:
            clean[section] = val

    if rejected:
        logger.info(f"🧼 [MemoryQuarantine] {speaker}: 剔除 {len(rejected)} 項——{'; '.join(rejected[:5])}")
    return clean, rejected

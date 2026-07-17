"""TTS length policy — LLM 產出超過 task 預算秒數時的截斷防線。

Why：LLM prompt 寫「3 秒內」是 soft 指示，模型不一定聽話。music intro（歌名+點播者）
若被吹到 30 字，3 秒預算會破，整個切歌節奏被拉長。本 module 在 TTS engine 入口前
攔一道：估時長 > policy → 在最近的符號處切；無符號則硬切 + 省略號。

設計原則：
- pure function：duration 估算注入（測試不需綁 TTS engine）
- 截斷時優先保持語意完整：在 [budget-3, budget+2] 字範圍內找符號，切到符號前（不含）
- 都沒符號 → 硬切 budget 字 + 「⋯」標記
- task 不在 policy 表 / policy=None → 不截（fail-safe，未知 task 不該被悄悄裁掉）
"""
from __future__ import annotations

from typing import Callable, Optional


# 各 task 的 TTS 時長硬上限（秒）。None = 無限制（不 gate）。
LIMITS: dict[str, Optional[float]] = {
    "music_intro": 5.0,      # DJ 播報（歌名+理由+點播者一句）：≤5s，別打斷切歌節奏
                             # （autopilot phrase / themed 理由 / fallback 共用）
                             # 2026-05-24 7s→15s(DJ 發揮)；2026-07-13 15s→5s(使用者：DJ 話太多)
    "dj_story": 18.0,        # human 點歌的 DJ 串場：說故事不唸資訊 → 放寬（2026-07-15）
                             # ⚠️ 這是「估算器秒數」非真實秒數：估算器保守(0.3s/中文字
                             # 含×1.2)、真實 edge-tts ≈0.17s/字。18s≈60字上限＝真實≈10s。
                             # 想改「幾秒」先換算：真實秒數 ×1.8 才是這裡要填的數字，
                             # 照字面填 10.0 會在 33 字砍斷（真實 5.7s）＝殘句重演。
                             # 2026-07-15 27s；2026-07-17 →18s（使用者：雞湯文改成 10 秒）
                             # live 實測 LLM 常超寫（24 則 9 則爆 gate），這網真的會用到
    "callback": 15.0,        # Memory callback（必須講到聽懂，但別變嘮叨）
                             # 2026-05-24 從 7s 拉到 15s
    "marvin_reply": None,    # 主回覆，不 hard gate（caller 自己控）
    "scrap": 3.0,            # 通用短 scrap（report_sent / joke_request / footer 等）
}

# 中文常見句讀（按優先順序：句末符號最優先，逗點次之，列點頓號最次）
_PUNCT_CHARS = "。！？!?，,、；;…⋯"

# 找不到 budget 內符號時，允許往後尋找的字數（避免完全硬切）
_BUDGET_CEIL_TOLERANCE  = 2


def _budget_chars(limit_sec: float, est_duration_fn: Callable[[str], float]) -> int:
    """反推 budget 字數：用 estimator 對長度 100 字採樣得平均秒/字。"""
    sample = "字" * 100
    per_char = est_duration_fn(sample) / 100 if est_duration_fn(sample) > 0 else 0.3
    return max(1, int(limit_sec / per_char))


def truncate_for_tts(
    text: str,
    task: str,
    estimate_duration_fn: Callable[[str], float],
) -> tuple[str, bool]:
    """估時長超 task 上限 → 在符號處截斷；無符號則硬切 + 「⋯」。

    切點選擇：
      1. budget 內（index 0..budget）由右到左找最後一個符號 → 切到符號前
      2. 找不到 → budget+1..budget+ceil 容忍區內找第一個符號 → 切到符號前
      3. 還是沒有 → 硬切 budget 字 + 「⋯」

    Returns: (truncated_text, was_truncated)
    """
    if not text:
        return text, False

    limit = LIMITS.get(task)
    if limit is None:
        return text, False  # 未知 task 或無限制：原文回

    if estimate_duration_fn(text) <= limit:
        return text, False

    budget = _budget_chars(limit, estimate_duration_fn)

    # Step 1：budget 內由後往前找最後一個符號（保留最多內容）
    search_end = min(budget, len(text) - 1)
    for i in range(search_end, 0, -1):  # i > 0 避免切成空字串
        if text[i] in _PUNCT_CHARS:
            return text[:i], True

    # Step 2：budget+1..budget+ceil 容忍區內找第一個符號（小幅超 budget 換乾淨切）
    for i in range(budget + 1, min(len(text), budget + 1 + _BUDGET_CEIL_TOLERANCE)):
        if text[i] in _PUNCT_CHARS:
            return text[:i], True

    # Step 3：找不到符號 → 硬切到 budget 字 + 省略號
    return text[:budget] + "⋯", True

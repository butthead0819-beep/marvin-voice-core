"""TTS length policy — LLM 產出超過 task 預算秒數時的截斷防線。

Why：LLM prompt 寫「3 秒內」是 soft 指示，模型不一定聽話。music intro（歌名+點播者）
若被吹到 30 字，3 秒預算會破，整個切歌節奏被拉長。本 module 在 TTS engine 入口前
攔一道：估時長 > policy → 在最近的符號處切；無符號則硬切 + 省略號。

設計原則：
- pure function：duration 估算注入（測試不需綁 TTS engine）
- 截斷時優先保持語意完整：在 [budget-3, budget+2] 字範圍內找符號，切到符號前（不含）
- 句尾符號優先（保留符號本身，含收尾引號）；逗號類只是句尾找不到或太短時的退路
- 都沒符號 → 硬切 budget 字 + 「⋯」標記
- task 不在 policy 表 / policy=None → 不截（fail-safe，未知 task 不該被悄悄裁掉）
"""
from __future__ import annotations

from typing import Callable, Optional

from persona_loader import load_tts_limits

# 各 task 的 TTS 時長硬上限（秒）。None = 無限制（不 gate）。
# 數值本體 + 踩坑註解在 personas/tts_limits.yaml（改「幾秒」前先讀那份註解）。
LIMITS: dict[str, Optional[float]] = load_tts_limits()

# 中文常見句讀（按優先順序：句末符號最優先，逗點次之，列點頓號最次）
_PUNCT_CHARS = "。！？!?，,、；;…⋯"

# 句尾符號（Step 0 優先找這個，切點保留符號本身）
_SENTENCE_END_CHARS = "。！？!?…⋯"
# 緊接句尾符號後的收尾引號，一起帶上避免「」不成對
_CLOSING_QUOTES = "」』”\""
# 句尾切點至少要保留 budget 的 1/3，太短就退回逗號切法
_MIN_SENTENCE_FRACTION = 1 / 3

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
      0. budget 內由右到左找最後一個句尾符號（。！？!?…⋯）→ 切到符號後（保留符號，
         含緊接的收尾引號）；切點需 >= budget * 1/3，太短則退回下面的逗號切法
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

    # Step 0：budget 內由右到左找最後一個句尾符號，保留符號＋收尾引號整段切下
    for i in range(min(budget, len(text) - 1), 0, -1):
        if text[i] in _SENTENCE_END_CHARS:
            j = i + 1
            while j < len(text) and text[j] in _CLOSING_QUOTES:
                j += 1
            if j >= budget * _MIN_SENTENCE_FRACTION:
                return text[:j], True
            break

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

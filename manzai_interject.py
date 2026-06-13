"""打岔切入時機計算（pure）。

算 Marmo 該在 Marvin 講到哪個比例切進來：從 base 比例出發，微調到落在「子句中段、
離標點夠遠」的字元位置——讓打岔像真的打斷（切句中），而非剛好在標點換句處接話。

時間 ≈ 字元位置（假設語速大致均勻）；對「避開停頓點」的目的夠用。
"""
from __future__ import annotations

_PUNCT = set("。，、！？；：,.!?;:… 　")

_FRAME_S = 0.02  # discord 一幀 = 20ms


def interject_diagnostics(
    *,
    at_ratio: float,
    est_dur_s: float,
    marvin_frames: int,
    marmo_frames: int,
    marmo_first_chunk_s: float,
) -> dict:
    """把一次打岔疊播的原始量測換算成可讀時機診斷（pure）。

    設計上 Marmo 該在 Marvin 講到 `at_ratio` 處切入，但切入點是乘在「估算時長」
    `est_dur_s` 上、且 Marmo TTS 首塊有生成延遲——兩者都讓「耳朵聽到的切入點」
    偏離設計值。此函式把這些換算成可直接和設計比例對照的秒數/比例。

    回 dict：
      marvin_actual_s   — Marvin 實際播放長度（frames × 20ms）
      marmo_actual_s    — Marmo 實際播放長度
      trigger_s         — Marmo task 啟動時點（= est_dur × at_ratio）
      perceived_entry_s — 耳朵真正聽到 Marmo 的時點（trigger + 首塊延遲）
      perceived_ratio   — perceived_entry / marvin_actual（與設計 at 對照；marvin 無播放 → 0.0）
      overlap_s         — Marvin 還在講時與 Marmo 真正重疊的秒數（≤0 = 幾乎沒重疊，等於接話）
    """
    marvin_actual_s = marvin_frames * _FRAME_S
    marmo_actual_s = marmo_frames * _FRAME_S
    trigger_s = est_dur_s * at_ratio
    perceived_entry_s = trigger_s + marmo_first_chunk_s
    perceived_ratio = perceived_entry_s / marvin_actual_s if marvin_actual_s > 0 else 0.0
    overlap_s = marvin_actual_s - perceived_entry_s if marvin_actual_s > 0 else 0.0
    return {
        "marvin_actual_s": marvin_actual_s,
        "marmo_actual_s": marmo_actual_s,
        "trigger_s": trigger_s,
        "perceived_entry_s": perceived_entry_s,
        "perceived_ratio": perceived_ratio,
        "overlap_s": overlap_s,
    }


def compute_interject_ratio(text: str, base: float = 0.72, min_gap: int = 2) -> float:
    """回 Marmo 切入的時間比例 (0~1)。

    從 base×len 的目標字元出發，若太靠近標點（< min_gap 字）→ 往兩側找最近的
    「離所有標點 ≥min_gap 且本身非標點」的字元，回它的比例。text 太短 → 直接回 base。
    """
    n = len(text or "")
    if n < 4:
        return base
    puncts = {i for i, c in enumerate(text) if c in _PUNCT}
    target = max(1, min(n - 1, round(base * n)))

    def ok(p: int) -> bool:
        return 0 <= p < n and p not in puncts and all(abs(p - q) >= min_gap for q in puncts)

    if ok(target):
        return target / n
    for d in range(1, n):
        for p in (target - d, target + d):
            if ok(p):
                return p / n
    return base  # 整句都是標點密集 / 找不到 → 退回 base

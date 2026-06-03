"""打岔切入時機計算（pure）。

算 Marmo 該在 Marvin 講到哪個比例切進來：從 base 比例出發，微調到落在「子句中段、
離標點夠遠」的字元位置——讓打岔像真的打斷（切句中），而非剛好在標點換句處接話。

時間 ≈ 字元位置（假設語速大致均勻）；對「避開停頓點」的目的夠用。
"""
from __future__ import annotations

_PUNCT = set("。，、！？；：,.!?;:… 　")


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

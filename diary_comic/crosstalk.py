"""搶話熱度偵測：有人在別人還沒講完時插入、且自己也講長 = 全場最投入的時刻。

純函式、只吃 (speaker, text, ts) 時序。靠「開始時間貼近 + 兩段都夠長」判高信心重疊，
濾掉禮讓性附和（說幾個字就停）。給策展層選 Hero 用。
"""
from __future__ import annotations

from dataclasses import dataclass, field

MIN_SUB = 8     # 持續發言門檻（字）：低於此視為附和、不算搶話
GAP = 2.0       # 兩段開始相差 ≤ 此秒數 = 講到一半被插（一句長話不可能 2 秒講完）


@dataclass
class CrosstalkPeak:
    heat: float
    ts: float
    speakers: list[str]
    lines: list[tuple[str, str]] = field(default_factory=list)  # (speaker, text)


def crosstalk_peak(rows, min_sub: int = MIN_SUB, gap: float = GAP) -> CrosstalkPeak | None:
    """回該場最熱搶話事件，無則 None。

    heat = 同時講長的不同人數 + 話長破同分（上限 0.9 → 人數永遠主導）。
    """
    n = len(rows)
    best: CrosstalkPeak | None = None
    for i in range(n):
        s1, t1, ts1 = rows[i]
        if len(t1) < min_sub:
            continue
        grp = {s1}
        chars = len(t1)
        last = ts1
        members = [(s1, t1)]
        for j in range(i + 1, n):
            s2, t2, ts2 = rows[j]
            if ts2 - last > gap:
                break
            if len(t2) < min_sub:
                continue
            grp.add(s2)
            chars += len(t2)
            last = ts2
            members.append((s2, t2))
        if len(grp) >= 2:
            heat = len(grp) + min(chars / 300.0, 0.9)
            if best is None or heat > best.heat:
                best = CrosstalkPeak(heat=heat, ts=ts1, speakers=sorted(grp), lines=members)
    return best

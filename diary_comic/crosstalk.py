"""搶話熱度偵測：有人在別人還沒講完時插入、且自己也講長 = 全場最投入的時刻。

純函式、只吃 (speaker, text, ts) 時序。靠「開始時間貼近 + 兩段都夠長」判高信心重疊，
濾掉禮讓性附和（說幾個字就停）。

兩種出口（共用同一道掃描 _crosstalk_events，單一真相、不漂移）：
  - crosstalk_peak  → 全場最熱單一事件（curator 選 Hero 用，行為與舊版逐筆一致）
  - crosstalk_track → 整場依時間 bin 的熱度序列（夜晚回放秀 EKG 用）

資料流：
  rows ─_crosstalk_events→ [CrosstalkPeak...] ─┬─ max ──→ crosstalk_peak
                                               └─ bin ──→ crosstalk_track ─→ pick_hottest_window
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


def _crosstalk_events(rows, min_sub: int = MIN_SUB, gap: float = GAP) -> list[CrosstalkPeak]:
    """掃出所有「有人插話 + 兩段都夠長」的搶話事件（依起點 ts 升序）。

    每個 i（自己講長）為起點，貪婪併入後續 gap≤gap 的長句，group≥2 即一個事件。
    heat = 同時講長的不同人數 + 話長破同分（上限 0.9 → 人數永遠主導）。
    這是 crosstalk_peak / crosstalk_track 唯一共用的掃描來源。
    """
    n = len(rows)
    events: list[CrosstalkPeak] = []
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
            events.append(CrosstalkPeak(heat=heat, ts=ts1, speakers=sorted(grp), lines=members))
    return events


def crosstalk_peak(rows, min_sub: int = MIN_SUB, gap: float = GAP) -> CrosstalkPeak | None:
    """回該場最熱搶話事件，無則 None。

    行為與舊版逐筆一致：事件依 ts 升序產生，平手取「最先」那個（舊版用 `>` 只在嚴格更大時更新）。
    """
    best: CrosstalkPeak | None = None
    for ev in _crosstalk_events(rows, min_sub, gap):
        if best is None or ev.heat > best.heat:   # 嚴格大於 → 平手保留最先
            best = ev
    return best


def crosstalk_track(rows, bin_s: float = 10.0, min_sub: int = MIN_SUB,
                    gap: float = GAP) -> list[tuple[float, float]]:
    """整場熱度時間序列：每個 bin 取該 bin 內事件 heat 的 max（非 sum，避免長窗灌水）。

    回 [(bin_start_ts, heat)...]，涵蓋首末事件之間每個 bin（無事件的 bin heat=0.0，讓 EKG 有谷）。
    無事件 → []（平淡夜訊號，給 pick_hottest_window 退場用）。
    """
    events = _crosstalk_events(rows, min_sub, gap)
    if not events:
        return []
    ts0 = min(ev.ts for ev in events)
    ts1 = max(ev.ts for ev in events)
    nbins = int((ts1 - ts0) // bin_s) + 1
    bins = [0.0] * nbins
    for ev in events:
        idx = int((ev.ts - ts0) // bin_s)
        if ev.heat > bins[idx]:
            bins[idx] = ev.heat
    return [(ts0 + k * bin_s, bins[k]) for k in range(nbins)]


def activity_track(rows, bin_s: float = 30.0) -> list[tuple[float, float]]:
    """發言密度時間序列：每個 bin 的句數（不分長短、不論是否搶話）。

    這是「熱鬧」的訊號——一群人輪流講也算熱鬧，跟 crosstalk（只算搶話）互補。
    回 [(bin_start_ts, count)...]，空 rows → []。呼叫端應先濾掉 bot 自己的句。
    """
    if not rows:
        return []
    ts0 = rows[0][2]
    ts1 = rows[-1][2]
    nbins = int((ts1 - ts0) // bin_s) + 1
    counts = [0] * nbins
    for _spk, _txt, ts in rows:
        counts[int((ts - ts0) // bin_s)] += 1
    return [(ts0 + k * bin_s, float(counts[k])) for k in range(nbins)]


def pick_hottest_window(track, win_s: float = 120.0) -> tuple[float, float] | None:
    """從熱度序列挑「最熱的固定長度窗」(start_ts, end_ts)，置中在最高 heat 的 bin。

    空 track → None（平淡夜 fallback：呼叫端退靜態海報、不出圖）。
    win_s 固定 → 整晚 3-5h 不線性壓成孤立尖刺，只渲這段最熱窗。
    """
    if not track:
        return None
    peak_t = max(track, key=lambda th: th[1])[0]
    start = peak_t - win_s / 2.0
    return (start, start + win_s)

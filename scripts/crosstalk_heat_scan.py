"""搶話熱度選擇器（離線驗證）：跨全歷史 transcript，每場挑「最熱搶話時刻」。

熱度 = 同時講長(≥MIN_SUB字)且開始貼近(≤GAP秒)的不同人數 + 話長破同分(上限0.9)。
人數為主 → 3人混戰永遠 > 2人。每場取 max。看分布決定「夠高」門檻。

用法：venv_simon/bin/python scripts/crosstalk_heat_scan.py [幾天內]
"""
import datetime
import sqlite3
import sys

MIN_SUB = 8     # 持續發言門檻（字）：濾掉禮讓性附和
GAP = 2.0       # 兩段開始相差 ≤ 此秒數 = 講到一半被插（高信心重疊）
SESSION_GAP = 1800  # 30 分鐘無人說話 = 換場


def crosstalk_peak(rows):
    """rows=(speaker,text,ts) 時序。回該場最熱事件 (heat, ts, n_people, members) 或 None。"""
    n = len(rows)
    best = None
    for i in range(n):
        s1, t1, ts1 = rows[i]
        if len(t1) < MIN_SUB:
            continue
        grp = {s1}; chars = len(t1); last = ts1; members = [(s1, t1, ts1)]
        for j in range(i + 1, n):
            s2, t2, ts2 = rows[j]
            if ts2 - last > GAP:
                break
            if len(t2) < MIN_SUB:
                continue
            grp.add(s2); chars += len(t2); last = ts2; members.append((s2, t2, ts2))
        if len(grp) >= 2:
            heat = len(grp) + min(chars / 300.0, 0.9)
            if best is None or heat > best[0]:
                best = (heat, ts1, len(grp), members)
    return best


def split_sessions(rows):
    out, cur = [], []
    for r in rows:
        if cur and r[2] - cur[-1][2] > SESSION_GAP:
            out.append(cur); cur = []
        cur.append(r)
    if cur:
        out.append(cur)
    return out


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 3650
    con = sqlite3.connect("marvin.db")
    cut = datetime.datetime.now().timestamp() - days * 86400
    rows = con.execute("SELECT speaker, text, timestamp FROM transcripts "
                       "WHERE timestamp >= ? ORDER BY timestamp", (cut,)).fetchall()
    con.close()

    sessions = [s for s in split_sessions(rows) if len(s) >= 20]  # 太短的場次跳過
    peaks = []
    for s in sessions:
        pk = crosstalk_peak(s)
        if pk:
            peaks.append((s, pk))

    print(f"共 {len(rows)} 句 / {len(sessions)} 場（≥20句）/ {len(peaks)} 場有搶話峰值\n")

    # 峰值熱度分布
    heats = sorted(p[1][0] for p in peaks)
    if heats:
        def q(f): return heats[min(len(heats) - 1, int(len(heats) * f))]
        print(f"每場峰值熱度分布: min={heats[0]:.2f} p50={q(0.5):.2f} "
              f"p75={q(0.75):.2f} p90={q(0.9):.2f} max={heats[-1]:.2f}")
        n3 = sum(1 for p in peaks if p[1][2] >= 3)
        print(f"峰值是 3+ 人混戰的場次: {n3}/{len(peaks)}\n")

    # 最近 12 場的峰值
    print("== 最近 12 場的最熱搶話時刻 ==")
    for s, (heat, ts, npl, members) in peaks[-12:]:
        when = datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
        print(f"\n[{when}] 熱度={heat:.2f}　{npl}人")
        for sp, t, r in members[:4]:
            print(f"   {r-ts:+.1f}s {sp}: {t[:34]}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""core_principles_weekly.py — Marvin 核心規範週報（docs/MARVIN_CORE_PRINCIPLES.md §4 / G6）。

把七條原則的指標表自動算出來，對照前一週。純讀 records/*.jsonl，不打 LLM、不發 Discord。
沒資料的指標顯示「無資料」，不捏造 0%。

用法：
  python scripts/core_principles_weekly.py                       # [今天-7天, 今天 00:00)，對照前 7 天
  python scripts/core_principles_weekly.py --end 2026-10-26 --days 7
  python scripts/core_principles_weekly.py --root /path          # records 所在專案根目錄
輸出：stdout + records/core_principles_weekly_<end>.md
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 本地日界（同 daily_user_needs.py）
TPE = timezone(timedelta(hours=8))

# 來源 docs/MARVIN_CORE_PRINCIPLES.md；改門檻兩邊一起改
THRESHOLDS = {
    "recall_rate_min": 0.90,
    "marvin_skip_rate_max": 0.05,
    "hot_chat_unrequested_max": 0,
    "wake_p50_max_s": 5.0,
    "false_responding_rate_max": 0.05,
    "proactive_p95_max_s": 5.0,
    "proactive_per_hour_max_s": 60.0,
    "requested_p95_max_s": 15.0,
    "min_presence_hours": 10,
}
MEMORY_DIRECT_LANES = {"spotlight", "long_tail", "group_resonance"}


# ── pure core ────────────────────────────────────────────────────────────────

def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def percentile(vals: list, p: float):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(int(len(s) * p), len(s) - 1)]


def _in(row: dict, s: float, e: float) -> bool:
    ts = row.get("ts")
    return isinstance(ts, (int, float)) and s <= ts < e


def _metric(rows: list[dict], name: str, s: float, e: float) -> list[dict]:
    return [r for r in rows if r.get("metric") == name and _in(r, s, e)]


def _rate(n: int, hit: int) -> dict:
    return {"n": n, "hit": hit, "rate": hit / n} if n else {"n": 0}


def memory_recall(qm: list[dict], s: float, e: float) -> dict:
    rows = _metric(qm, "recall", s, e)
    return _rate(len(rows), sum(1 for r in rows if r.get("correct") is True))


def marvin_skip_rate(plays: list[dict], skips: list[dict], s: float, e: float) -> dict:
    def is_marvin(r):
        return "Marvin" in (r.get("requested_by") or "")
    p = [r for r in plays if r.get("type") == "play" and _in(r, s, e)]
    k = [r for r in skips if _in(r, s, e)]
    out = _rate(sum(1 for r in p if is_marvin(r)), sum(1 for r in k if is_marvin(r)))
    out["human"] = _rate(sum(1 for r in p if not is_marvin(r)), sum(1 for r in k if not is_marvin(r)))
    return out


def pick_lanes(recs: list[dict], s: float, e: float) -> dict:
    lanes = Counter((r.get("channel_state") or {}).get("lane") or "unknown"
                    for r in recs if r.get("agent") == "music" and _in(r, s, e))
    n = sum(lanes.values())
    if not n:
        return {"n": 0}
    direct = sum(c for lane, c in lanes.items() if lane in MEMORY_DIRECT_LANES)
    return {"n": n, "lanes": dict(lanes), "memory_direct_share": direct / n}


def _unrequested_played(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("verdict") == "play" and r.get("requested") is False]


def hot_chat_unrequested(qm: list[dict], s: float, e: float) -> dict:
    rows = _metric(qm, "speech", s, e)
    if not rows:
        return {"n": 0}
    played = _unrequested_played(rows)
    return {
        "n": len(played),
        "hot": sum(1 for r in played if r.get("hot_chat") is True),
        "over_speech": sum(1 for r in played if (r.get("n_speaking") or 0) > 0),
        "blocked": sum(1 for r in rows if r.get("verdict") in ("drop", "drop_to_text")),
    }


def wake_latency(fb: list[dict], s: float, e: float) -> dict:
    vals = []
    for r in fb:
        try:
            ts = datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TPE).timestamp()
        except Exception:
            continue
        v = r.get("wake_latency_sec")
        if s <= ts < e and isinstance(v, (int, float)):
            vals.append(float(v))
    if not vals:
        return {"n": 0}
    return {"n": len(vals), "p50": percentile(vals, 0.5), "p90": percentile(vals, 0.9)}


def false_responding(qm: list[dict], s: float, e: float) -> dict:
    rows = _metric(qm, "false_responding", s, e)
    return _rate(len(rows), sum(1 for r in rows if r.get("was_false") is True))


def proactive_voice(qm: list[dict], s: float, e: float, *, presence_hours: int) -> dict:
    rows = _metric(qm, "speech", s, e)
    played = [r for r in rows if r.get("verdict") == "play"]
    pro = [r for r in played if r.get("requested") is False]
    req = [r for r in played if r.get("requested") is True]
    req_d = [r["dur_s"] for r in req if isinstance(r.get("dur_s"), (int, float))]
    requested = {"n": len(req), "p95": percentile(req_d, 0.95)} if req else {"n": 0}
    if not pro:
        return {"n": 0, "requested": requested}
    d = [r["dur_s"] for r in pro if isinstance(r.get("dur_s"), (int, float))]
    total = sum(d)
    return {
        "n": len(pro),
        "p95": percentile(d, 0.95),
        "total_s": total,
        "per_presence_hour": (total / presence_hours) if presence_hours else None,
        "inferred_share": sum(1 for r in pro if r.get("kind_inferred")) / len(pro),
        "requested": requested,
    }


def memory_checks(rows: list[dict], s: float, e: float) -> dict:
    in_window = [r for r in rows if _in(r, s, e)]
    asked = sum(1 for r in in_window if r.get("event") == "asked")
    answers = [r for r in in_window if r.get("event") == "answer"]
    yes = sum(1 for r in answers if r.get("answer") == "yes")
    no = sum(1 for r in answers if r.get("answer") == "no")
    n = yes + no
    if n == 0:
        return {"n": 0, "asked": asked, "answered": 0}
    return {"n": n, "hit": yes, "rate": yes / n, "asked": asked, "answered": n}


def presence_hours(qm: list[dict], s: float, e: float) -> int:
    hours = {datetime.fromtimestamp(r["ts"], TPE).strftime("%Y-%m-%d %H")
             for r in _metric(qm, "stt_confidence", s, e) if r.get("empty") is False}
    return len(hours)


def compute(data: dict, s: float, e: float) -> dict:
    qm = data.get("qm", [])
    ph = presence_hours(qm, s, e)
    return {
        "presence_hours": ph,
        "recall": memory_recall(qm, s, e),
        "skip": marvin_skip_rate(data.get("plays", []), data.get("skips", []), s, e),
        "lanes": pick_lanes(data.get("recs", []), s, e),
        "hot": hot_chat_unrequested(qm, s, e),
        "latency": wake_latency(data.get("fb", []), s, e),
        "false": false_responding(qm, s, e),
        "voice": proactive_voice(qm, s, e, presence_hours=ph),
        "mcheck": memory_checks(data.get("mchecks", []), s, e),
    }


# ── render ───────────────────────────────────────────────────────────────────

def _pct(d: dict) -> str:
    return f"{d['rate'] * 100:.1f}% ({d['hit']}/{d['n']})" if d.get("n") else "無資料"


def _sec(v) -> str:
    return f"{v:.1f}s" if isinstance(v, (int, float)) else "無資料"


def _mark(ok) -> str:
    return "—" if ok is None else ("✅" if ok else "❌")


def _rows(m: dict) -> list[tuple[str, str, object]]:
    """(列名, 顯示值, 是否過門檻 None=不判/無資料)"""
    T = THRESHOLDS
    r, sk, la, h, lat, fr, v, mc = (m["recall"], m["skip"], m["lanes"], m["hot"],
                                    m["latency"], m["false"], m["voice"], m["mcheck"])
    vr = v.get("requested", {"n": 0})
    out = [
        ("1 記憶正確率", _pct(r), (r["rate"] >= T["recall_rate_min"]) if r.get("n") else None),
        ("1b 現場確認記憶", _pct(mc), (mc["rate"] >= T["recall_rate_min"]) if mc.get("n") else None),
        ("2a Marvin 推薦 skip 率", _pct(sk), (sk["rate"] <= T["marvin_skip_rate_max"]) if sk.get("n") else None),
        ("2b 直接取自記憶的選歌", f"{la['memory_direct_share'] * 100:.1f}% (n={la['n']})" if la.get("n") else "無資料", None),
        ("3 熱聊時主動出聲", f"{h['hot']} 次 / 主動 {h['n']}" if h.get("n") else "無資料",
         (h["hot"] <= T["hot_chat_unrequested_max"]) if h.get("n") else None),
        ("4 喚醒延遲 p50/p90", f"{lat['p50']:.1f}s / {lat['p90']:.1f}s (n={lat['n']})" if lat.get("n") else "無資料",
         (lat["p50"] <= T["wake_p50_max_s"]) if lat.get("n") else None),
        ("5 誤回應率", _pct(fr), (fr["rate"] <= T["false_responding_rate_max"]) if fr.get("n") else None),
    ]
    if v.get("n"):
        pph = v["per_presence_hour"]
        ok6 = (v["p95"] is not None and v["p95"] <= T["proactive_p95_max_s"]
               and pph is not None and pph <= T["proactive_per_hour_max_s"])
        out.append(("6 主動發聲 p95／每有人小時", f"{_sec(v['p95'])} / {_sec(pph)}", ok6 if v["p95"] is not None else None))
    else:
        out.append(("6 主動發聲 p95／每有人小時", "無資料", None))
    out.append(("6b 被請求回答 p95", _sec(vr.get("p95")) if vr.get("n") else "無資料",
                (vr["p95"] <= T["requested_p95_max_s"]) if vr.get("n") and vr.get("p95") is not None else None))
    out.append(("7 主動發話滿意度", "🔴 量測點未接（G3）", None))
    return out


def render(cur: dict, prev: dict, start_label: str, end_label: str) -> str:
    T = THRESHOLDS
    lines = [f"# Marvin 核心規範週報 {start_label} → {end_label}",
             f"樣本：有人小時數 本週 {cur['presence_hours']} / 上週 {prev['presence_hours']}"]
    if cur["presence_hours"] < T["min_presence_hours"]:
        lines.append("⚠️ 樣本不足，本週不下結論")
    lines += ["", "| 原則 | 本週 | 上週 | 判定 |", "|---|---|---|---|"]
    cur_rows, prev_rows = _rows(cur), _rows(prev)
    both_empty = []
    for (name, val, ok), (_, pval, _) in zip(cur_rows, prev_rows):
        lines.append(f"| {name} | {val} | {pval} | {_mark(ok)} |")
        if val == "無資料" and pval == "無資料":
            both_empty.append(name)
    lines += ["", "## 明細"]
    la = cur["lanes"]
    if la.get("n"):
        parts = [f"{k} {c / la['n'] * 100:.1f}% ({c})" for k, c in sorted(la["lanes"].items(), key=lambda x: -x[1])]
        lines.append("- 2b lane 分佈：" + " / ".join(parts))
    sk = cur["skip"]
    lines.append(f"- 2a 人工點歌 skip 率（對照）：{_pct(sk.get('human', {'n': 0}))}")
    h = cur["hot"]
    if h.get("n"):
        lines.append(f"- 3 開口時有人在講（主動）：{h['over_speech']} 次；被擋下的發話：{h['blocked']} 次")
    v = cur["voice"]
    if v.get("n"):
        lines.append(f"- 6 主動發聲總秒數：{v['total_s']:.1f}s；kind 由舊旗標推測的比例：{v['inferred_share'] * 100:.1f}%")
    mc = cur["mcheck"]
    lines.append(f"- 1b 提問 {mc.get('asked', 0)} 次，有明確回答 {mc.get('answered', 0)} 次")
    lines.append("- 連續兩週無資料：" + ("、".join(both_empty) if both_empty else "無"))
    lines.append(f"- 門檻來源：docs/MARVIN_CORE_PRINCIPLES.md（喚醒 p50 ≤ {T['wake_p50_max_s']}s 為階段一）")
    return "\n".join(lines) + "\n"


# ── IO shell ─────────────────────────────────────────────────────────────────

def _load(root: Path) -> dict:
    rec = root / "records"
    return {
        "qm": load_jsonl(rec / "quality_metrics.jsonl"),
        "plays": load_jsonl(rec / "song_plays.jsonl"),
        "skips": load_jsonl(rec / "song_skips.jsonl"),
        "recs": load_jsonl(rec / "agent_recommendations.jsonl"),
        "fb": load_jsonl(rec / "response_feedback.jsonl"),
        "mchecks": load_jsonl(rec / "memory_checks.jsonl"),
    }


def main(argv: list[str] | None = None) -> str:
    ap = argparse.ArgumentParser()
    ap.add_argument("--end", help="區間結束日 YYYY-MM-DD（不含當天），預設今天")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--root", default=str(ROOT))
    a = ap.parse_args(argv)
    if a.end:
        end_dt = datetime.strptime(a.end, "%Y-%m-%d").replace(tzinfo=TPE)
    else:
        end_dt = datetime.now(TPE).replace(hour=0, minute=0, second=0, microsecond=0)
    span = timedelta(days=a.days)
    e, s, ps = end_dt.timestamp(), (end_dt - span).timestamp(), (end_dt - 2 * span).timestamp()
    root = Path(a.root)
    data = _load(root)
    md = render(compute(data, s, e), compute(data, ps, s),
                (end_dt - span).strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"))
    print(md)
    out = root / "records" / f"core_principles_weekly_{end_dt.strftime('%Y-%m-%d')}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    return md


if __name__ == "__main__":
    main()

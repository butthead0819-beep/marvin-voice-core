#!/usr/bin/env python3
"""Plan trigger condition checker — 每天 3:00 跑，stdout 一段 markdown table
給 run_feedback_batch.py append 到當天 records/feedback_analysis_<date>.md 末尾。

純 file stat + jsonl 計數，無 LLM、無外部 I/O。設計成可獨立跑、無 side effect。

對應 records/plans/plan_01..09 的 trigger condition。手動判斷類（Plan 5/6/7）標 manual。
"""
from __future__ import annotations

import json
import pathlib
from datetime import date, datetime, timedelta

REPO = pathlib.Path(__file__).resolve().parent.parent
RECORDS = REPO / "records"


def _count_jsonl(path: pathlib.Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as f:
        return sum(1 for _ in f)


def _count_recent_jsonl(path: pathlib.Path, days: int) -> dict[date, int]:
    today = date.today()
    counts = {today - timedelta(days=i): 0 for i in range(days)}
    if not path.exists():
        return counts
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
                ts = rec.get("ts") or rec.get("timestamp")
                if ts is None:
                    continue
                d = datetime.fromtimestamp(float(ts)).date()
                if d in counts:
                    counts[d] += 1
            except Exception:
                continue
    return counts


def check_judge_race_unlock() -> dict:
    """Plan 1/2/3/9 trigger: 6/1 之後跑過 analyze_judge_outcomes 產生 analysis report。"""
    today = date.today()
    target = date(2026, 6, 1)
    days_to = (target - today).days
    if days_to > 0:
        return {
            "plan": "1/2/3/9",
            "title": "Judge race 6/1 重收分析鏈",
            "trigger": "6/1 後跑 analyze_judge_outcomes",
            "current": f"等 {days_to} 天到 6/1",
            "met": False,
        }
    has_report = list(RECORDS.glob("judge_outcomes_analysis_2026-06-*.md"))
    if has_report:
        return {
            "plan": "1/2/3/9",
            "title": "Judge race 6/1 重收分析鏈",
            "trigger": "6/1 後跑 analyze_judge_outcomes",
            "current": f"已產 {len(has_report)} 份 6/+ 分析",
            "met": True,
        }
    return {
        "plan": "1/2/3/9",
        "title": "Judge race 6/1 重收分析鏈",
        "trigger": "6/1 後跑 analyze_judge_outcomes",
        "current": "已過 6/1 但 analysis report 未生成",
        "met": False,
    }


def check_intent_gap_clustering() -> dict:
    """Plan 4 trigger: agent_gaps.jsonl 內 non-UNKNOWN ≥5 筆。

    UNKNOWN 是 classifier 對「無意圖雜訊」的合法輸出（見 intent_gap.py
    IntentGapRecord docstring），對它跑 clustering 永遠是空 cluster。
    只有真有 intent 字串時 clustering 才有料可合併。
    """
    path = RECORDS / "agent_gaps.jsonl"
    count = 0
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("intent_type") and rec["intent_type"] != "UNKNOWN":
                    count += 1
    return {
        "plan": 4,
        "title": "Intent Gap Phase A.5 Clustering",
        "trigger": "agent_gaps.jsonl non-UNKNOWN ≥5 筆",
        "current": f"累積 {count} 筆 non-UNKNOWN",
        "met": count >= 5,
    }


def check_j1_improvement_loop() -> dict:
    """Plan 8 trigger: 過去 7 天 judge_outcomes 每天 ≥30 樣本。"""
    daily = _count_recent_jsonl(RECORDS / "judge_outcomes.jsonl", 7)
    daily_list = [daily[d] for d in sorted(daily)]
    all_above = bool(daily_list) and all(c >= 30 for c in daily_list)
    avg = sum(daily_list) / 7 if daily_list else 0
    return {
        "plan": 8,
        "title": "J1 三條改善迴圈",
        "trigger": "7 天每天 ≥30 樣本",
        "current": f"過去 7 天平均 {avg:.1f}/天 ({','.join(str(c) for c in daily_list)})",
        "met": all_above,
    }


def manual_plans() -> list[dict]:
    return [
        {
            "plan": 5,
            "title": "ProactiveArbiter 3 發言者遷移",
            "trigger": "STT/wake 穩定性 incident 平息",
            "current": "需人類判斷",
            "met": None,
        },
        {
            "plan": 6,
            "title": "別記/別說 同意線",
            "trigger": "返場 callback 上線 OR 真實 incident",
            "current": "需人類判斷",
            "met": None,
        },
        {
            "plan": 7,
            "title": "MemoryCallback embedding",
            "trigger": "callback win/天 <1（14 天觀察）",
            "current": "speak_outcomes schema 無 callback type，需 sample 判斷",
            "met": None,
        },
    ]


def render_markdown(results: list[dict]) -> str:
    today = date.today().isoformat()
    lines = [
        "",
        "---",
        "",
        "## 📋 Plan trigger status snapshot",
        "",
        f"_{today} · `scripts/check_plan_triggers.py` 自動產生_",
        "",
        "| Plan | Title | Trigger | Current | Status |",
        "|------|-------|---------|---------|--------|",
    ]
    for r in results:
        if r["met"] is True:
            status = "✅ **達成**"
        elif r["met"] is False:
            status = "⏸ 未達"
        else:
            status = "👤 manual"
        lines.append(
            f"| {r['plan']} | {r['title']} | {r['trigger']} | {r['current']} | {status} |"
        )
    lines.append("")
    lines.append("dashboard: `records/marvin_status_dashboard.html`")
    lines.append("")
    return "\n".join(lines)


def main():
    results = [
        check_judge_race_unlock(),
        check_intent_gap_clustering(),
        *manual_plans(),
        check_j1_improvement_loop(),
    ]
    print(render_markdown(results))


if __name__ == "__main__":
    main()

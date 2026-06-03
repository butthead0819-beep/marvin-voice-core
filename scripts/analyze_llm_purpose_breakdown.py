#!/usr/bin/env python3
"""每日 LLM 呼叫歸因報表（3am batch）。

三個數字（承 2026-06-03 #1/#2/#3）：
  1. per-purpose 量 + 成敗 + 失敗原因分類（records/llm_routing.jsonl，#1 後 purpose 真實）
  2. cleaner 截斷 JSON 救援命中率（🔧 救回 vs ⚠️ 降級 raw，#2）
  3. 過矯正次數（cleaner 注入喚醒詞被拒，#2 prompt 收緊後應下降）

純函式（categorize_error / aggregate_by_purpose / count_cleaner_events）可單測；
main() 做 IO + 寫報告。無資料誠實回報，不捏造。

用法：
  python scripts/analyze_llm_purpose_breakdown.py [--date YYYY-MM-DD]
  launchd（3am batch）不帶參數 → 預設昨天。
"""
from __future__ import annotations

import argparse
import collections
import datetime as _dt
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
LLM_ROUTING_PATH = BASE_DIR / "records" / "llm_routing.jsonl"
# cleaner 的 logger.warning/info（過矯正 / JSON 救援）落 launchd StandardOutPath，
# 單檔跨多日 append（非專案根那個只收 print 類 timing 的 bot_stdout.log）。
CLEANER_LOG_PATH = Path.home() / "Library" / "Logs" / "Marvin" / "bot_stdout.log"

# 背景 purpose（與 llm_agents.base.BACKGROUND_PURPOSES 對齊；此處複製避免 import 重依賴）
_BACKGROUND = frozenset({
    "extract_memory", "batch_extract_memories", "audit_player_memory",
    "extract_emotional_moments", "analyze_social_dynamics", "analyze_tactical_situation",
    "update_toxicity", "summarize_window", "_classify_mood", "compress",
    "marvinize_news", "generate_song_blueprint",
})


# ── pure core ─────────────────────────────────────────────────────────────────

def categorize_error(err) -> str:
    """失敗 error 字串 → 粗分類。空字串/None → 'ok'。"""
    e = str(err or "")
    if not e:
        return "ok"
    el = e.lower()
    if "429" in e or "rate" in el or "quota" in el or "queue" in el:
        return "429_限流"
    if "404" in e or "does not exist" in e or "not_found" in e:
        return "404_模型下架"
    if any(c in e for c in ("500", "502", "503")) or "server" in el:
        return "5xx_server"
    if "no_llm" in e or "below threshold" in e or "cooldown" in e:
        return "no_llm_池冷卻"
    if "timeout" in el:
        return "timeout"
    return "other"


def _row_day(d: dict) -> str | None:
    ts = d.get("ts")
    if not isinstance(ts, (int, float)):
        return None
    try:
        return _dt.datetime.fromtimestamp(ts).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def aggregate_by_purpose(rows: list[dict], day: str) -> dict:
    """回 {purpose: {"ok": int, "fail": int, "reasons": Counter}}（只取 day 當天）。"""
    out: dict[str, dict] = {}
    for d in rows:
        if _row_day(d) != day:
            continue
        purpose = d.get("purpose") or "?"
        slot = out.setdefault(purpose, {"ok": 0, "fail": 0, "reasons": collections.Counter()})
        if d.get("success") is True:
            slot["ok"] += 1
        else:
            slot["fail"] += 1
            slot["reasons"][categorize_error(d.get("error"))] += 1
    return out


def count_cleaner_events(lines, day: str) -> dict:
    """grep 當天 cleaner 事件 → {recovered, json_failed, overcorrection}。"""
    recovered = json_failed = overcorrection = 0
    for ln in lines:
        if not ln.startswith(day):
            continue
        if "JSON 截斷，救回" in ln:
            recovered += 1
        elif "JSON 解析失敗，降級純文字" in ln:
            json_failed += 1
        if "過矯正" in ln or "注入喚醒詞" in ln:
            overcorrection += 1
    return {"recovered": recovered, "json_failed": json_failed, "overcorrection": overcorrection}


def build_report(rows: list[dict], log_lines: list[str], day: str) -> str:
    agg = aggregate_by_purpose(rows, day)
    cle = count_cleaner_events(log_lines, day)
    L = [f"# LLM 呼叫歸因 — {day}", ""]

    if not agg:
        L.append("（當天 llm_routing.jsonl 無 _call_llm 呼叫 — 沒人對話 / bot 未啟動 / #1 未生效。不捏造。）")
        L.append("")
    else:
        total_ok = sum(v["ok"] for v in agg.values())
        total_fail = sum(v["fail"] for v in agg.values())
        total = total_ok + total_fail
        bg_calls = sum(v["ok"] + v["fail"] for p, v in agg.items() if p in _BACKGROUND)
        L.append(f"**{total}** 筆 `_call_llm`（成功 {total_ok} / 失敗 {total_fail}，"
                 f"失敗率 {100 * total_fail / total:.0f}%）；其中背景 purpose {bg_calls} 筆 "
                 f"({100 * bg_calls / total:.0f}%)。")
        L.append("")
        L.append("## per-purpose（量降序）")
        L.append("")
        L.append("| purpose | 總量 | 成功 | 失敗 | 背景? |")
        L.append("|---|---:|---:|---:|:--:|")
        for p, v in sorted(agg.items(), key=lambda x: -(x[1]["ok"] + x[1]["fail"])):
            n = v["ok"] + v["fail"]
            L.append(f"| {p} | {n} | {v['ok']} | {v['fail']} | {'✅' if p in _BACKGROUND else ''} |")
        L.append("")
        # 失敗原因彙整
        all_reasons: collections.Counter = collections.Counter()
        for v in agg.values():
            all_reasons.update(v["reasons"])
        if all_reasons:
            L.append("## 失敗原因分類（cat-3 server 拒絕為主）")
            L.append("")
            for r, c in all_reasons.most_common():
                L.append(f"- {c}× {r}")
            L.append("")

    # cleaner 健康（#2 修法成效）
    L.append("## cleaner 健康（#2 修法成效）")
    L.append("")
    rec, jf, oc = cle["recovered"], cle["json_failed"], cle["overcorrection"]
    denom = rec + jf
    if denom:
        L.append(f"- 截斷 JSON 救援：救回 **{rec}** / 仍降級 raw {jf} "
                 f"（救援率 {100 * rec / denom:.0f}%）")
    else:
        L.append("- 截斷 JSON 救援：當天無截斷事件（或 log 已輪轉）")
    L.append(f"- 過矯正（注入喚醒詞被拒）：**{oc}** 次"
             + ("（prompt 收緊後觀察是否下降）" if oc else "（讚，當天 0 次）"))
    L.append("")
    return "\n".join(L) + "\n"


# ── IO shell ─────────────────────────────────────────────────────────────────

def _read_llm_routing(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def _read_cleaner_log_lines(log_path: Path) -> list[str]:
    """讀 cleaner log（單檔 append），只留含 cleaner 標記的行（省記憶體）。"""
    if not log_path.exists():
        return []
    out: list[str] = []
    with open(log_path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if "STT Clean" in line:
                out.append(line.rstrip("\n"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="每日 LLM 呼叫歸因報表")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD（預設昨天）")
    args = ap.parse_args()

    day = args.date or (_dt.date.today() - _dt.timedelta(days=1)).isoformat()

    rows = _read_llm_routing(LLM_ROUTING_PATH)
    log_lines = _read_cleaner_log_lines(CLEANER_LOG_PATH)
    report = build_report(rows, log_lines, day)

    out_path = BASE_DIR / "records" / f"llm_purpose_breakdown_{day}.md"
    out_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"[llm_purpose] → {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

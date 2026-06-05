#!/usr/bin/env python3
"""分析 voice pipeline 延遲組成，定位 baseline 2-3s 遲鈍感卡在哪段。

三個資料源拼出「使用者停話 → STT → cleaner → 回應 LLM → TTS 首音 → 播放」鏈：
  1. [STAGE_TIMING] log line（前半：endpoint→STT→cleaner→intent_dispatched）
  2. [TTS_TIMING]   log line（TTS 合成→首音 byte）
  3. records/llm_routing.jsonl（回應 LLM latency + 成功率）

純函式（parser / percentile / stage 拆解）可單測；main() 做 IO + 寫報告。
無資料時誠實回報「沒人對話」，不捏造。

用法：
  python scripts/analyze_latency_breakdown.py [--date YYYY-MM-DD] [--hours N]
launchd（3am batch）呼叫時不帶參數，預設看過去 24h。
"""
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
# print() 重導向後 [STAGE_TIMING] / [TTS_TIMING] 都落 WORKDIR/bot_stdout.log（5MB
# RotatingFileHandler，輪轉成 .1/.2/.3）。非 ~/Library 那個 launchd StandardOutPath。
LOG_PATH = BASE_DIR / "bot_stdout.log"
LLM_ROUTING_PATH = BASE_DIR / "records" / "llm_routing.jsonl"

_STAGE_KEYS = ("sttstart", "sttdone", "dequeued", "questiondone", "cleanerdone", "intentdispatched", "total")
_STAGE_RE = re.compile(r"\[STAGE_TIMING\]")
_TTS_RE = re.compile(r"\[TTS_TIMING\]")
_KV_MS_RE = re.compile(r"(\w+)=(\d+)ms")


# ── pure parsers ─────────────────────────────────────────────────────────────


def parse_stage_timing(line: str) -> dict | None:
    """一行 [STAGE_TIMING] → {stage: ms}。非該類 line 回 None。"""
    if not _STAGE_RE.search(line):
        return None
    out: dict[str, int] = {}
    for k, v in _KV_MS_RE.findall(line):
        if k in _STAGE_KEYS:
            out[k] = int(v)
    return out or None


def parse_tts_timing(line: str) -> dict | None:
    """一行 [TTS_TIMING] → {first_audio, chars}。非該類 line 回 None。"""
    if not _TTS_RE.search(line):
        return None
    out: dict[str, int] = {}
    for k, v in _KV_MS_RE.findall(line):
        if k == "first_audio":
            out["first_audio"] = int(v)
    m = re.search(r"chars=(\d+)", line)
    if m:
        out["chars"] = int(m.group(1))
    return out or None


def stage_durations(record: dict) -> dict:
    """累積 ms（從 endpoint 起算）→ 各段 duration。缺的 stage 跳過。"""
    d: dict[str, int] = {}
    if "sttstart" in record:
        d["pre_stt"] = record["sttstart"]
    if "sttstart" in record and "sttdone" in record:
        d["stt"] = record["sttdone"] - record["sttstart"]
    # 有中間打點 → 把舊的混合 cleaner 段拆三段（排隊 / 等問句 / 真清洗）。
    # 無中間打點（舊 log 行 / nowake 等不過 confirm 的 route）→ 維持 legacy 單一 cleaner，向後相容。
    _has_split = "dequeued" in record or "questiondone" in record
    if _has_split:
        if "sttdone" in record and "dequeued" in record:
            d["queue_wait"] = record["dequeued"] - record["sttdone"]
        if "dequeued" in record and "questiondone" in record:
            d["question_wait"] = record["questiondone"] - record["dequeued"]
        if "questiondone" in record and "cleanerdone" in record:
            d["cleaner_pure"] = record["cleanerdone"] - record["questiondone"]
    elif "sttdone" in record and "cleanerdone" in record:
        d["cleaner"] = record["cleanerdone"] - record["sttdone"]
    if "cleanerdone" in record and "intentdispatched" in record:
        d["intent"] = record["intentdispatched"] - record["cleanerdone"]
    return d


def percentile(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, int(len(s) * p))]


def filter_recent(rows: list[dict], *, since_ts: float) -> list[dict]:
    return [r for r in rows if r.get("ts", 0) >= since_ts]


# ── IO helpers ───────────────────────────────────────────────────────────────


_LINE_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def line_timestamp(line: str) -> float | None:
    """從 log 行首 "YYYY-MM-DD HH:MM:SS" 解出 epoch；無法解析回 None。"""
    m = _LINE_TS_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def _read_log_lines(path: Path, since_ts: float | None = None) -> list[str]:
    """讀主檔 + 輪轉備份（.1/.2/.3），只回含 timing tag 的行。

    RotatingFileHandler 5MB 輪轉，timing 訊號會散在主檔與備份；只讀主檔會漏掉
    輪轉出去的那部分（這是 2026-06-02 STAGE_TIMING 報 0 的真因之一）。

    since_ts：給定時，依行首時間戳濾窗，與 llm_routing 的 24h 窗一致（避免舊
    故障期資料灌水）。無時間戳的行保守保留。
    """
    out: list[str] = []
    # 主檔 + .1/.2/.3（backupCount 預設 3，多撈幾個也無妨）
    candidates = [path] + [path.with_name(path.name + f".{i}") for i in range(1, 6)]
    for p in candidates:
        if not p.exists():
            continue
        with open(p, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if "STAGE_TIMING" not in line and "TTS_TIMING" not in line:
                    continue
                if since_ts is not None:
                    lt = line_timestamp(line)
                    if lt is not None and lt < since_ts:
                        continue
                out.append(line.rstrip("\n"))
    return out


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


def _fmt_pct(xs: list[float], unit: str = "ms") -> str:
    if not xs:
        return "no data"
    return (
        f"n={len(xs)} p50={percentile(xs, 0.5):.0f}{unit} "
        f"p90={percentile(xs, 0.9):.0f}{unit} max={max(xs):.0f}{unit}"
    )


def build_report(stage_lines: list[str], llm_rows: list[dict], since_ts: float, label: str) -> str:
    stage_recs = [r for r in (parse_stage_timing(l) for l in stage_lines) if r]
    tts_recs = [r for r in (parse_tts_timing(l) for l in stage_lines) if r]
    llm_recent = filter_recent(llm_rows, since_ts=since_ts)

    lines = [f"# Voice Latency Breakdown — {label}", ""]

    if not stage_recs and not tts_recs and not llm_recent:
        lines.append("（過去視窗內無 STAGE_TIMING / TTS_TIMING / LLM dispatch 資料 — ")
        lines.append("沒人對話，或 bot 未啟動。不捏造數據。）")
        return "\n".join(lines) + "\n"

    # 各段 duration 聚合
    by_stage: dict[str, list[int]] = {}
    for rec in stage_recs:
        for k, v in stage_durations(rec).items():
            by_stage.setdefault(k, []).append(v)
    tts_first = [r["first_audio"] for r in tts_recs if "first_audio" in r]
    llm_ok = [r["latency_ms"] for r in llm_recent if r.get("success") and r.get("latency_ms") is not None]
    llm_succ = sum(1 for r in llm_recent if r.get("success"))

    lines.append("## 鏈各段延遲（使用者停話 → 首音）")
    lines.append("")
    _STAGE_LABEL = {
        "pre_stt": "endpoint→STT 開始",
        "stt": "STT 轉錄",
        "queue_wait": "排隊等 worker",
        "question_wait": "等使用者講完問句",
        "cleaner_pure": "cleaner LLM（純清洗）",
        "cleaner": "cleaner LLM（舊量法,含排隊+等問句）",
        "intent": "intent dispatch",
    }
    stage_summary: list[tuple[str, float]] = []  # (label, p50) 找大頭
    # 跳過無樣本的段：新舊量法（cleaner_pure vs cleaner）共存於輪轉過渡期，
    # 全新後 legacy cleaner 自然降為 0 樣本而消失。
    for key in ("pre_stt", "stt", "queue_wait", "question_wait", "cleaner_pure", "cleaner", "intent"):
        xs = by_stage.get(key, [])
        if not xs:
            continue
        lab = _STAGE_LABEL[key]
        lines.append(f"- **{lab}**: {_fmt_pct(xs)}")
        p = percentile(xs, 0.5)
        if p is not None:
            stage_summary.append((lab, p))
    lines.append(f"- **回應 LLM**（llm_routing 成功）: {_fmt_pct(llm_ok)}"
                 + (f"  | 成功率 {llm_succ}/{len(llm_recent)} ({llm_succ/len(llm_recent):.0%})" if llm_recent else ""))
    if percentile(llm_ok, 0.5) is not None:
        stage_summary.append(("回應 LLM", percentile(llm_ok, 0.5)))
    lines.append(f"- **TTS 首音**: {_fmt_pct(tts_first)}")
    if percentile(tts_first, 0.5) is not None:
        stage_summary.append(("TTS 首音", percentile(tts_first, 0.5)))

    # 大頭歸因
    lines.append("")
    lines.append("## 歸因")
    if stage_summary:
        stage_summary.sort(key=lambda x: -x[1])
        top = stage_summary[0]
        total_p50 = sum(p for _, p in stage_summary)
        lines.append(f"- 最大段：**{top[0]}** (p50≈{top[1]:.0f}ms，"
                     f"佔可量到鏈的 {top[1]/total_p50:.0%})")
        lines.append(f"- 可量到鏈 p50 合計 ≈ {total_p50:.0f}ms")
        lines.append("  - 註：STAGE_TIMING（前半）與 LLM/TTS（後半）來自不同 timing context，"
                     "非同一 turn 串接；此處是各段中位數相加的近似，非端到端實測。")
    else:
        lines.append("- 資料不足以歸因（部分段無樣本）。")

    # 樣本數提醒
    lines.append("")
    lines.append("## 樣本數")
    lines.append(f"- STAGE_TIMING turns: {len(stage_recs)}")
    lines.append(f"- TTS_TIMING samples: {len(tts_recs)}")
    lines.append(f"- LLM dispatch（視窗內）: {len(llm_recent)}")
    if len(stage_recs) < 10:
        lines.append("- ⚠️ 樣本 <10，數據參考性弱，等更多對話再看。")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Voice pipeline latency breakdown")
    ap.add_argument("--hours", type=float, default=24.0, help="回看幾小時（預設 24）")
    ap.add_argument("--date", default=None, help="報告標籤日期（預設今天）")
    args = ap.parse_args()

    now = time.time()
    since_ts = now - args.hours * 3600
    label = args.date or datetime.now().strftime("%Y-%m-%d")

    stage_lines = _read_log_lines(LOG_PATH, since_ts=since_ts)
    llm_rows = _read_llm_routing(LLM_ROUTING_PATH)

    report = build_report(stage_lines, llm_rows, since_ts, label)

    out_path = BASE_DIR / "records" / f"latency_breakdown_{label}.md"
    out_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"[latency] → {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

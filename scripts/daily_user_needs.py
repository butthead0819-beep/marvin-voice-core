"""daily_user_needs.py — 每日 ritual：逐筆撈出昨天使用者的「需求」與「抱怨」訊號。

純報表產生器，不打 LLM、不寫檔。每日起手儀式（見 memory daily_feedback_ritual）跑這支，
把輸出貼給 Jack，Claude 對每筆附一句改善建議，Jack 人工評估要不要做。

三個訊源：
  1. 未滿足需求  — records/agent_gaps.jsonl（有 intent 但沒 agent / 被模板 ack 打發）
  2. 抱怨 / 挫折  — marvin.db transcripts 表，比對 frustration_agent 的關鍵字 + 幾個補充
  3. AmbientQA   — records/ambient_qa.jsonl（若存在；grounded 問答上線後的逐筆）

用法：
  python scripts/daily_user_needs.py                # 昨天
  python scripts/daily_user_needs.py --date 2026-08-29
  python scripts/daily_user_needs.py --days 3       # 最近 3 天
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GAPS = ROOT / "records" / "agent_gaps.jsonl"
AMBIENT = ROOT / "records" / "ambient_qa.jsonl"
RESCUE = ROOT / "records" / "rescue_outcomes.jsonl"
DB = ROOT / "marvin.db"

# 測試 speaker：ritual 一律排除（見 memory intent_health_check_2026-06-22）
EXCLUDE_SPEAKERS = {"artificial", "test", "TestSpeaker", "測試"}

# 抱怨偵測心得（2026-08-30 實測）：直接對 raw transcript 跑 frustration regex 沒用——
# 這頻道是朋友閒聊，「你在幹嘛 / 搞什麼 / 聽不懂 / 到底想講什麼」全是人-對-人對話，
# 7 天 61→15 命中真正對馬文的 <3。真正乾淨的抱怨訊號有兩個：
#   1. rescue_outcomes.jsonl 的 pragmatic_signal=="negative"（跑過 pipeline、有語境分析）
#   2. 喚醒 token + 明確故障詞 同句（「馬文沒聲音」）——乾淨但這頻道幾乎為 0
_WAKE_RE = re.compile(r"馬文|把文|毛文|麻文|媽文|馬聞")
_FAULT_RE = re.compile(
    r"沒反應|沒回應|沒聲音|沒回話|不理我|跳針|重複播|重複講|當機|卡住|壞掉"
    r"|又錯了|還是錯|聽不懂我(?:講|說)|沒聽到我(?:講|說)|失靈|沒動靜|又沒反應",
    re.IGNORECASE,
)


def _day_bounds(date_str: str | None, days: int) -> tuple[float, float, str]:
    if date_str:
        end = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
        start = end - timedelta(days=days)
        label = date_str if days == 1 else f"{start.date()}~{(end - timedelta(days=1)).date()}"
    else:
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        end = today
        start = end - timedelta(days=days)
        label = str((end - timedelta(days=1)).date()) if days == 1 else f"{start.date()}~{(end - timedelta(days=1)).date()}"
    return start.timestamp(), end.timestamp(), label


def _load_jsonl(path: Path, lo: float, hi: float) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = rec.get("ts") or rec.get("timestamp") or 0
        if lo <= ts < hi:
            out.append(rec)
    return out


def section_needs(lo: float, hi: float) -> str:
    rows = [r for r in _load_jsonl(GAPS, lo, hi) if r.get("speaker") not in EXCLUDE_SPEAKERS]
    if not rows:
        return "### 1. 未滿足需求（agent_gaps）\n\n_無_\n"
    by_type: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_type[r.get("intent_type") or "UNKNOWN"].append(r)

    lines = ["### 1. 未滿足需求（agent_gaps）\n"]
    lines.append(f"共 {len(rows)} 筆，{len(by_type)} 類意圖。UNKNOWN（多為 STT 糊字 / 馬文自己的話被誤記）排最後、只列 3 筆。\n")
    # 非 UNKNOWN 先、按量排；UNKNOWN 一律最後
    ordered = sorted(
        by_type.items(),
        key=lambda kv: (kv[0] == "UNKNOWN", -len(kv[1])),
    )
    for itype, items in ordered:
        # 同 (speaker, raw_query) 去重
        seen = set()
        uniq = []
        for it in items:
            key = (it.get("speaker"), it.get("raw_query"))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(it)
        acked = sum(1 for it in items if it.get("acknowledged"))
        nearest = items[0].get("nearest_agent")
        domain = items[0].get("query_domain")
        hdr = f"**`{itype}`** — {len(items)} 筆 / {len(uniq)} distinct"
        meta = []
        if nearest:
            meta.append(f"nearest={nearest}")
        if domain:
            meta.append(f"domain={domain}")
        meta.append(f"{acked}/{len(items)} 被模板 ack")
        cap = 3 if itype == "UNKNOWN" else 8
        lines.append(f"{hdr}（{', '.join(meta)}）")
        for it in uniq[:cap]:
            t = datetime.fromtimestamp(it.get("ts", 0)).strftime("%m-%d %H:%M")
            lines.append(f"  - `{it.get('speaker')}` {t}：「{it.get('raw_query', '')[:80]}」")
        if len(uniq) > cap:
            lines.append(f"  - …還有 {len(uniq) - cap} 筆")
        lines.append("")
    return "\n".join(lines)


def section_complaints(lo: float, hi: float) -> str:
    lines = ["### 2. 對馬文的不滿 / 挫折\n"]

    # 2a — rescue_outcomes 的 pragmatic negative（跑過 pipeline、有語境）
    stale = ""
    if RESCUE.exists():
        last = RESCUE.stat().st_mtime
        if time.time() - last > 3 * 86400:
            stale = f"  ⚠️ rescue_outcomes.jsonl 最後寫入 {datetime.fromtimestamp(last):%Y-%m-%d}——訊號源可能斷了，查 MARVIN_INTENT_RESCUE_SHADOW"
    neg = [r for r in _load_jsonl(RESCUE, lo, hi)
           if r.get("pragmatic_signal") == "negative" and r.get("speaker") not in EXCLUDE_SPEAKERS]
    lines.append(f"**2a. pragmatic negative（rescue_outcomes）** — {len(neg)} 筆")
    if stale:
        lines.append(stale)
    if neg:
        by_target: dict[str, list] = defaultdict(list)
        for r in neg:
            by_target[r.get("pragmatic_target") or "?"].append(r)
        for target, items in sorted(by_target.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"  對 `{target}`：{len(items)} 筆")
            for r in items[:10]:
                t = datetime.fromtimestamp(r.get("ts", 0)).strftime("%m-%d %H:%M")
                lines.append(f"    - `{r.get('speaker')}` {t}：「{r.get('original_query', '')[:70]}」")
    else:
        lines.append("  _無_")
    lines.append("")

    # 2b — 喚醒 token + 明確故障詞 同句（乾淨但這頻道通常為 0）
    lines.append("**2b. 「馬文…（故障詞）」同句（transcripts）**")
    if not DB.exists():
        lines.append("  _marvin.db 不存在_\n")
        return "\n".join(lines)
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        cur = con.execute(
            "SELECT speaker, text, timestamp FROM transcripts "
            "WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
            (lo, hi),
        )
        hits = [
            (s, txt.strip(), ts) for s, txt, ts in cur
            if s not in EXCLUDE_SPEAKERS and txt
            and _WAKE_RE.search(txt) and _FAULT_RE.search(txt)
        ]
    finally:
        con.close()
    if hits:
        for s, txt, ts in hits:
            t = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
            lines.append(f"  - `{s}` {t}：「{txt[:120]}」")
    else:
        lines.append("  _無_（這頻道通常如此；有命中一定要看）")
    lines.append("")
    return "\n".join(lines)


def section_ambient(lo: float, hi: float) -> str:
    rows = _load_jsonl(AMBIENT, lo, hi)
    if not AMBIENT.exists():
        return ""  # 還沒上線，整段省略
    if not rows:
        return "### 3. AmbientQA（grounded 問答）\n\n_無_\n"
    answered = [r for r in rows if r.get("answer")]
    none_reasons: dict[str, int] = defaultdict(int)
    for r in rows:
        if not r.get("answer"):
            none_reasons[r.get("reason", "?")] += 1
    lines = ["### 3. AmbientQA（grounded 問答）\n",
             f"共 {len(rows)} 筆，{len(answered)} 有答。"]
    if none_reasons:
        lines.append("未答：" + "、".join(f"{k}×{v}" for k, v in none_reasons.items()))
    lines.append("")
    for r in answered[:20]:
        t = datetime.fromtimestamp(r.get("ts", 0)).strftime("%m-%d %H:%M")
        lat = r.get("latency_ms")
        lines.append(
            f"  - `{r.get('speaker')}` {t}：「{r.get('query', '')[:60]}」"
            f" → 「{r.get('answer', '')[:80]}」"
            + (f" ({lat}ms)" if lat else "")
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD（預設昨天）")
    ap.add_argument("--days", type=int, default=1, help="往回幾天（預設 1）")
    args = ap.parse_args()

    lo, hi, label = _day_bounds(args.date, args.days)
    print(f"# 使用者需求 / 抱怨掃描 — {label}\n")
    print(f"_訊源：agent_gaps.jsonl / marvin.db transcripts"
          f"{' / ambient_qa.jsonl' if AMBIENT.exists() else ''}；已排除測試 speaker_\n")
    print(section_needs(lo, hi))
    print(section_complaints(lo, hi))
    amb = section_ambient(lo, hi)
    if amb:
        print(amb)
    print("---")
    print("_下一步：Claude 對每筆附一句改善建議，Jack 人工評估要不要做。_")


if __name__ == "__main__":
    main()

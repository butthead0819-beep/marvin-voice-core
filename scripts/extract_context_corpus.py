"""Extract wake events + prior chronological context from daily logs.

輸入：records/daily/*.log
輸出：tests/fixtures/context_sweep_corpus.jsonl

每條：{ts, speaker, raw, wake_intent, prior_context: [{speaker, text}, ...]}

prior_context 是該 wake event 前 10 分鐘內、最多 10 條 utterance（任何 speaker
+ Marvin 自己的回應），時序排序、最早→最近。
"""
from __future__ import annotations

import json
import re
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[.,]\d+\s*-\s*")
_WAKE_RE = re.compile(r"\[⚡喚醒\] \[([^\]]+)\] raw='([^']*)' \| Track=([AB]) \| wake_intent=(\S+)")
_DEBOUNCED_RE = re.compile(r"\[([^\]]+)\] \(Debounced\) (.+)$")
_QUERY_OK_RE = re.compile(r"\[✅Query通過\] \[([^\]]+)\] gate_ok \| query='([^']*)'")
_BOT_RE = re.compile(r"\[BOT(?:降臨|嘲諷|→[^\]]*)?\][→]?\s*([^|]*)\s*\|?\s*(.*)$")
_PROACTIVE_RE = re.compile(r"\[(?:BOT嘲諷|BOT降臨|主動觀察)[^\]]*\]")


@dataclass
class Utterance:
    ts: datetime
    speaker: str
    text: str


@dataclass
class WakeRow:
    ts: datetime
    speaker: str
    raw: str
    wake_intent: float | None
    prior_context: list[Utterance]


def _parse_ts(line: str) -> datetime | None:
    m = _TS_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def parse_log(path: Path) -> tuple[list[Utterance], list[WakeRow]]:
    """Return (all_utterances_chronological, wake_events)."""
    utterances: list[Utterance] = []
    wake_events: list[tuple[datetime, str, str, float | None]] = []

    with path.open(encoding="utf-8") as fh:
        for line in fh:
            ts = _parse_ts(line)
            if ts is None:
                continue

            # 1. [⚡喚醒] — wake event 記下，等之後找 prior context
            m = _WAKE_RE.search(line)
            if m:
                speaker, raw, _track, intent_str = m.groups()
                if intent_str == "None":
                    wi = None
                else:
                    try:
                        wi = float(intent_str)
                    except ValueError:
                        wi = None
                wake_events.append((ts, speaker, raw, wi))
                # 喚醒 raw 也算 utterance（之後若有別的 wake 緊接著它就會是 context）
                utterances.append(Utterance(ts=ts, speaker=speaker, text=raw))
                continue

            # 2. [Debounced] — non-wake STT 也算 utterance
            m = _DEBOUNCED_RE.search(line)
            if m:
                speaker, text = m.groups()
                utterances.append(Utterance(ts=ts, speaker=speaker, text=text.strip()))
                continue

            # 3. [✅Query通過] — cleaned query 是 wake 後的內容，但對下一個 wake event 來說是上文
            m = _QUERY_OK_RE.search(line)
            if m:
                speaker, query = m.groups()
                utterances.append(Utterance(ts=ts, speaker=speaker, text=query))
                continue

            # 4. [BOT嘲諷] / [BOT降臨] — Marvin 的話
            if _PROACTIVE_RE.search(line):
                # 簡化：用 timestamp 之後的整段當 Marvin text，截 100 字
                after_dash = line.split("- ", 1)[-1].strip()
                # 去掉前綴標籤，只留正文
                text = re.sub(r"^\[[^\]]+\]\s*", "", after_dash)
                text = re.sub(r"^\[[^\]]+\]\s*", "", text)  # 兩層標籤
                if "|" in text:
                    text = text.split("|", 1)[1].strip()
                utterances.append(Utterance(ts=ts, speaker="Marvin", text=text[:200]))
                continue

    return utterances, wake_events


def build_rows(utterances: list[Utterance], wake_events: list, max_context: int = 10,
               window_minutes: int = 10) -> list[WakeRow]:
    rows: list[WakeRow] = []
    # 用 deque 維護 sliding window
    for w_ts, speaker, raw, wi in wake_events:
        cutoff = w_ts - timedelta(minutes=window_minutes)
        # 取 wake_ts 之前、cutoff 之後、不含當前 wake event 本身
        prior = [u for u in utterances
                 if u.ts < w_ts and u.ts >= cutoff and not (u.ts == w_ts and u.text == raw)]
        # 最近 max_context 條
        prior = prior[-max_context:]
        rows.append(WakeRow(ts=w_ts, speaker=speaker, raw=raw, wake_intent=wi, prior_context=prior))
    return rows


def main():
    if len(sys.argv) < 2:
        print("Usage: extract_context_corpus.py records/daily/*.log [more...]", file=sys.stderr)
        return 1

    log_paths = [Path(p) for p in sys.argv[1:]]
    all_utterances: list[Utterance] = []
    all_wake: list = []
    for p in log_paths:
        if not p.exists():
            print(f"skip missing: {p}", file=sys.stderr)
            continue
        u, w = parse_log(p)
        all_utterances.extend(u)
        all_wake.extend(w)

    all_utterances.sort(key=lambda x: x.ts)
    all_wake.sort()
    print(f"parsed {len(all_utterances)} utterances, {len(all_wake)} wake events", file=sys.stderr)

    rows = build_rows(all_utterances, all_wake)
    # 篩有 prior_context 的，且 raw 不空（無 context 沒得測 context size）
    rows = [r for r in rows if r.prior_context and r.raw.strip()]
    # 為避免太多重複句污染統計，每個 raw 只保留一條
    seen = set()
    deduped = []
    for r in rows:
        key = (r.speaker, r.raw)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    print(f"after dedup + has-context filter: {len(deduped)} rows", file=sys.stderr)

    out_path = REPO_ROOT / "tests" / "fixtures" / "context_sweep_corpus.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for r in deduped:
            fh.write(json.dumps({
                "ts": r.ts.isoformat(),
                "speaker": r.speaker,
                "raw": r.raw,
                "wake_intent_baseline": r.wake_intent,
                "prior_context": [{"speaker": u.speaker, "text": u.text} for u in r.prior_context],
            }, ensure_ascii=False) + "\n")
    print(f"wrote: {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

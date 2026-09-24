"""
一次性從歷史資料灌 departure_cues.json，讓 DeparturePredictor 一上線就有樣本可用，
不用每人各自從零累積 8 筆才能開始先送客（見 departure_predictor.py MIN_SAMPLES）。

資料來源：
  - data/voice_presence.jsonl：每個人的離場時間（event == "leave", is_bot=false）
  - stt_history.log：每句 STT，抓「下線/晚安/拜拜」等線索

合併後依時間序重放進 DeparturePredictor.observe() / on_leave()，邏輯完全重用
DeparturePredictor 本體（不另寫一份判斷），跑完存一次檔。

用法：
    python scripts/backfill_departure_cues.py [--out departure_cues.json]

bot 執行中也可跑：舊版 bot（尚未部署這次改動）不讀這個檔，跑這支腳本不影響
正在運行的 bot；新版部署前跑一次即可，讓 DeparturePredictor 一上線就有歷史樣本。

目標檔已存在時預設拒絕覆蓋（避免蓋掉線上已經累積的即時樣本），需要重跑就先手動
搬走舊檔。
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from departure_predictor import DeparturePredictor  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRESENCE_PATH = os.path.join(REPO_ROOT, "data", "voice_presence.jsonl")
STT_HISTORY_PATH = os.path.join(REPO_ROOT, "stt_history.log")

_STT_LINE_RE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ - \[([^\]]+)\] \(Debounced\) (.*)$"
)


def _load_leaves() -> list[tuple[float, str]]:
    leaves = []
    with open(PRESENCE_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("is_bot") or rec.get("event") != "leave":
                continue
            leaves.append((rec["ts"], rec["user_name"]))
    return leaves


def _load_utterances(min_ts: float) -> list[tuple[float, str, str]]:
    utterances = []
    with open(STT_HISTORY_PATH, encoding="utf-8", errors="ignore") as f:
        for line in f:
            m = _STT_LINE_RE.match(line.rstrip("\n"))
            if not m:
                continue
            ts_str, speaker, text = m.groups()
            try:
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").timestamp()
            except ValueError:
                continue
            if ts < min_ts:
                continue
            utterances.append((ts, speaker, text))
    return utterances


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", default=os.path.join(REPO_ROOT, "departure_cues.json"),
        help="輸出的 departure_cues.json 路徑（預設 repo 根目錄）",
    )
    args = parser.parse_args()

    if os.path.exists(args.out):
        print(f"❌ {args.out} 已存在，拒絕覆蓋。要重跑請先手動搬走舊檔。", file=sys.stderr)
        sys.exit(1)

    leaves = _load_leaves()
    if not leaves:
        print("❌ data/voice_presence.jsonl 沒有任何 leave 紀錄，中止。", file=sys.stderr)
        sys.exit(1)
    min_ts = min(ts for ts, _ in leaves)

    utterances = _load_utterances(min_ts)

    events = (
        [(ts, "utter", speaker, text) for (ts, speaker, text) in utterances]
        + [(ts, "leave", speaker, None) for (ts, speaker) in leaves]
    )
    # 同一時間戳：發話排在離場前
    events.sort(key=lambda e: (e[0], 0 if e[1] == "utter" else 1))

    pred = DeparturePredictor(path=args.out)
    speakers = set()
    for ts, kind, speaker, text in events:
        speakers.add(speaker)
        if kind == "utter":
            pred.observe(speaker, text, ts)
        else:
            pred.on_leave(speaker, ts, persist=False)

    if events:
        last_ts = events[-1][0]
        for speaker in speakers:
            pred._expire(speaker, last_ts)

    pred.save()

    print(f"✅ 已寫入 {args.out}\n")
    for speaker in sorted(speakers):
        n, p = pred.precision(speaker)
        trigger = "✅ 會觸發" if pred.should_prefarewell(speaker, float("inf")) else "—"
        print(f"  {speaker}: n={n} 命中率={p:.0%} {trigger}")


if __name__ == "__main__":
    main()

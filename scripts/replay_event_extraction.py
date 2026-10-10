#!/usr/bin/env python3
"""scripts/replay_event_extraction.py — 離線重播事件抽取評估（不寫 prod DB）。

從既有 marvin.db 的 transcripts（唯讀開啟）依 5 分鐘窗口重播 event_memory.extract_and_store，
寫進一個暫存的 EventStore（--out-db），跑完印出統計與逐筆事件，供人工核對抽取品質。

用法（在 bot 的 venv + 有 GEMINI_API_KEY / GOOGLE_API_KEY 的環境）：
  python scripts/replay_event_extraction.py --start 2026-10-01 --end 2026-10-07 --out-db /tmp/replay_events.db
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TAIPEI_TZ = timezone(timedelta(hours=8))
WINDOW_SECONDS = 300
_MIN_UTTERANCES = 3


async def _run() -> int:
    ap = argparse.ArgumentParser(description="離線重播事件抽取評估")
    ap.add_argument("--db", default="marvin.db", help="SQLite DB 路徑 (預設: marvin.db，唯讀開啟)")
    ap.add_argument("--guild-id", type=int, default=int(os.environ.get("GUILD_ID") or "0"))
    ap.add_argument("--start", required=True, help="YYYY-MM-DD（台北時區，含）")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD（台北時區，含）")
    ap.add_argument("--out-db", required=True, help="暫存 EventStore 路徑（若已存在先刪掉）")
    ap.add_argument("--max-windows", type=int, default=150)
    args = ap.parse_args()

    out_db_path = Path(args.out_db)
    if out_db_path.exists():
        out_db_path.unlink()

    start_dt = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=TAIPEI_TZ)
    end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=TAIPEI_TZ) + timedelta(days=1)
    start_ts = start_dt.timestamp()
    end_ts = end_dt.timestamp()

    os.environ.setdefault("LLM_BUS", "true")
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    from gemini_router import GeminiRouter

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        print("❌ 沒有 GEMINI_API_KEY / GOOGLE_API_KEY，無法跑抽取")
        return 1

    router = GeminiRouter(api_key)
    try:
        router._init_llm_bus()
    except Exception as e:
        print(f"[warn] bus init 失敗，走 legacy 直連: {e}")

    from event_memory import extract_and_store, EventStore
    from session_summarizer import SessionSummarizer

    db_uri = f"file:{Path(args.db).resolve()}?mode=ro"
    con = sqlite3.connect(db_uri, uri=True)
    rows = con.execute(
        "SELECT speaker, text, timestamp FROM transcripts "
        "WHERE guild_id = ? AND timestamp >= ? AND timestamp < ? ORDER BY timestamp ASC",
        (args.guild_id, start_ts, end_ts),
    ).fetchall()
    con.close()

    store = EventStore(str(out_db_path))
    script_start = time.time()

    total_added = total_merged = total_dropped = 0
    windows_done = 0
    window_start = start_ts
    idx = 0
    n = len(rows)

    while window_start < end_ts:
        window_end = window_start + WINDOW_SECONDS
        bucket = []
        while idx < n and rows[idx][2] < window_end:
            if rows[idx][2] >= window_start:
                bucket.append(rows[idx])
            idx += 1

        if len(bucket) < _MIN_UTTERANCES:
            window_start = window_end
            continue

        if windows_done >= args.max_windows:
            print(f"⚠️ 已達 --max-windows={args.max_windows}，停止重播")
            break

        utterances = [{"speaker": r[0], "text": r[1], "timestamp": r[2]} for r in bucket]
        transcript_text = SessionSummarizer._format_transcript(utterances)
        heard_by = []
        for u in utterances:
            if u["speaker"] not in heard_by:
                heard_by.append(u["speaker"])

        try:
            stats = await extract_and_store(router, store, args.guild_id, transcript_text, heard_by, window_end)
            total_added += stats["added"]
            total_merged += stats["merged"]
            total_dropped += stats["dropped"]
        except Exception as e:
            print(f"[窗口 {window_start}~{window_end}] 例外: {e}")

        windows_done += 1
        window_start = window_end

    print(f"\n處理窗數：{windows_done}")
    print(f"added={total_added} merged={total_merged} dropped={total_dropped}\n")

    events = store.list_guild(args.guild_id, 0)
    events.sort(key=lambda e: e["first_seen"])
    for e in events:
        date_str = (
            datetime.fromtimestamp(e["event_date"], TAIPEI_TZ).strftime("%Y-%m-%d")
            if e["event_date"] is not None else "-"
        )
        first_str = datetime.fromtimestamp(e["first_seen"], TAIPEI_TZ).strftime("%m-%d %H:%M")
        last_str = datetime.fromtimestamp(e["last_seen"], TAIPEI_TZ).strftime("%m-%d %H:%M")
        print(
            f"#{e['id']} {e['owner']}｜{e['activity']}｜{e['place'] or '-'}｜{date_str}｜"
            f"{e['date_text'] or '-'}｜heard_by={e['heard_by']}｜quotes={len(e['quotes'])} 句｜"
            f"{first_str}~{last_str}"
        )
        for q in e["quotes"]:
            print(f"    「{q}」")

    records_path = ROOT / "records" / "llm_routing.jsonl"
    if records_path.exists():
        import json as _json
        model_counts: dict[str, int] = {}
        with records_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = _json.loads(line)
                except Exception:
                    continue
                if rec.get("purpose") != "extract_events":
                    continue
                ts = rec.get("ts")
                if not isinstance(ts, (int, float)) or ts < script_start:
                    continue
                model = rec.get("model", "?")
                model_counts[model] = model_counts.get(model, 0) + 1
        print("\n--- extract_events model 分佈（本次執行期間）---")
        for model, cnt in sorted(model_counts.items(), key=lambda kv: -kv[1]):
            print(f"{model}: {cnt}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))

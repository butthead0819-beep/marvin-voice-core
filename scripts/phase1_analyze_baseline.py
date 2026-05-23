"""
phase1_analyze_baseline.py — Phase 1 M7 voice_presence.jsonl 分析

讀 data/voice_presence.jsonl (presence_logger.py 寫的 forward-looking voice
channel join/leave/move events)，算 P7 「presence as vote」指標：

  - per-user 每日總在線分鐘
  - 回流頻率：每週進 voice channel 的天數 / 人
  - 30-day rolling average 在線時長
  - 對照 phase0_baseline JSON proxy（從 transcript_store 算的 active engagement）
    顯示 Phase 1 ship 後 presence vs 啟動前 baseline 的 delta

Phase 1 ship 後跑 (每週 / 月底 evaluation gate 用):
  venv_simon/bin/python scripts/phase1_analyze_baseline.py
  venv_simon/bin/python scripts/phase1_analyze_baseline.py --json   # 給其它 tool
  venv_simon/bin/python scripts/phase1_analyze_baseline.py --days 30
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_PRESENCE_LOG = Path("data/voice_presence.jsonl")


def parse_session_durations(events: list[dict]) -> dict[str, dict[str, float]]:
    """
    從 events 解析 per-user per-day 累計在線分鐘。

    Events 排序: chronological。對每個 user，配對相鄰 (join, leave)：
      - join 開時間戳
      - leave 累加 duration 到 join 當天
      - move 視為 leave 舊 channel + join 新 channel
      - 跨日 session：duration 全算到 join 當天（簡化、避免日期切片）
      - 沒配對的 leave / dangling join (last event is join) → 忽略

    Returns: { user_id: { "YYYY-MM-DD": minutes_active, ... } }
    """
    by_user: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    open_session: dict[str, float] = {}  # user_id → 開始 ts

    for ev in sorted(events, key=lambda e: e.get("ts", 0)):
        if ev.get("is_bot"):
            continue
        user = ev.get("user_id")
        ts = float(ev.get("ts", 0) or 0)
        event_type = ev.get("event", "")

        if event_type == "join":
            open_session[user] = ts
        elif event_type == "leave":
            start_ts = open_session.pop(user, None)
            if start_ts is not None:
                day_key = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d")
                by_user[user][day_key] += (ts - start_ts) / 60.0
        elif event_type == "move":
            # treat as leave + join
            start_ts = open_session.get(user)
            if start_ts is not None:
                day_key = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d")
                by_user[user][day_key] += (ts - start_ts) / 60.0
            open_session[user] = ts

    # Dangling open sessions (user 還在線中) → 算到「現在」
    now_ts = datetime.now(timezone.utc).timestamp()
    for user, start_ts in open_session.items():
        if now_ts - start_ts > 30 * 86400:
            continue  # > 30 天 dangling 是 bug 或 bot 重啟前漏 leave，跳過
        day_key = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        by_user[user][day_key] += (now_ts - start_ts) / 60.0

    return {u: dict(d) for u, d in by_user.items()}


def get_user_display_names(events: list[dict]) -> dict[str, str]:
    """user_id → 最近一次出現的 display_name。"""
    name_map: dict[str, str] = {}
    for ev in events:
        uid = ev.get("user_id")
        name = ev.get("user_name")
        if uid and name:
            name_map[uid] = name
    return name_map


def analyze(log_path: Path, days: int) -> dict:
    """主分析邏輯。"""
    if not log_path.exists():
        return {"error": f"presence log not found: {log_path}"}

    events: list[dict] = []
    with log_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not events:
        return {"error": "presence log empty"}

    # Filter by --days window
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    events_in_window = [e for e in events if float(e.get("ts", 0) or 0) >= cutoff]

    per_user_per_day = parse_session_durations(events_in_window)
    name_map = get_user_display_names(events)

    # Per-user 統計
    per_user_summary = {}
    for uid, days_map in per_user_per_day.items():
        total_min = sum(days_map.values())
        active_days = sum(1 for m in days_map.values() if m > 0)
        avg_min_per_active_day = total_min / active_days if active_days > 0 else 0.0
        per_user_summary[uid] = {
            "display_name": name_map.get(uid, uid),
            "total_minutes": round(total_min, 1),
            "active_days": active_days,
            "avg_minutes_per_active_day": round(avg_min_per_active_day, 1),
            "return_freq": round(active_days / days, 3) if days > 0 else 0.0,
        }

    # 整體
    daily_active_users: dict[str, set[str]] = defaultdict(set)
    for uid, days_map in per_user_per_day.items():
        for day_key, mins in days_map.items():
            if mins > 0:
                daily_active_users[day_key].add(uid)

    avg_daily_active_users = (sum(len(s) for s in daily_active_users.values())
                              / len(daily_active_users)) if daily_active_users else 0.0

    sorted_users = sorted(per_user_summary.items(),
                          key=lambda x: -x[1]["total_minutes"])

    return {
        "log_path": str(log_path),
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "overall": {
            "total_events": len(events_in_window),
            "unique_users": len([u for u, s in per_user_summary.items() if s["active_days"] > 0]),
            "days_with_activity": len(daily_active_users),
            "avg_daily_active_users": round(avg_daily_active_users, 2),
        },
        "per_user": {uid: s for uid, s in sorted_users},
        "per_day_active_users": {
            day: sorted([name_map.get(uid, uid) for uid in users])
            for day, users in sorted(daily_active_users.items())
        },
    }


def find_phase0_baseline() -> Path | None:
    """找最近一份 data/phase0_baseline_*.json 做 delta 對照。"""
    data_dir = Path("data")
    if not data_dir.exists():
        return None
    candidates = sorted(data_dir.glob("phase0_baseline_*.json"))
    return candidates[-1] if candidates else None


def compute_delta(phase1: dict, phase0_path: Path) -> dict | None:
    """對照 phase0 proxy baseline 算 delta（粗略：active_days_ratio 比較）。"""
    try:
        with phase0_path.open("r", encoding="utf-8") as f:
            phase0 = json.load(f)
    except Exception:
        return None

    # Phase 0 (proxy from transcript) 是 active speaker（有發言）
    # Phase 1 (real presence) 是 has-joined-voice
    # 兩個 metric 不同類，delta 只是 directional indicator
    p0_avg = phase0.get("overall", {}).get("avg_daily_active_speakers", 0)
    p1_avg = phase1.get("overall", {}).get("avg_daily_active_users", 0)

    return {
        "phase0_path": str(phase0_path),
        "phase0_avg_daily_active_speakers": p0_avg,
        "phase1_avg_daily_active_users": p1_avg,
        "delta_users": round(p1_avg - p0_avg, 2),
        "note": "phase0 = 有發言 (proxy); phase1 = 進 voice channel (P7 真值); 兩者 metric 不同類，僅 directional",
    }


def main():
    parser = argparse.ArgumentParser(description="Phase 1 voice_presence baseline analyzer")
    parser.add_argument("--log", type=str, default=str(DEFAULT_PRESENCE_LOG))
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    log_path = Path(args.log)
    result = analyze(log_path, args.days)

    # Try compute delta against phase0 baseline
    if "error" not in result:
        phase0_path = find_phase0_baseline()
        if phase0_path:
            delta = compute_delta(result, phase0_path)
            if delta:
                result["phase0_delta"] = delta

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if "error" in result:
        print(f"⚠️  {result['error']}")
        if "presence log" in result["error"]:
            print(f"   (Phase 1 還沒 ship 前是空的；presence_logger.py wire 後從 bot 重啟開始累積)")
        return

    print("=" * 60)
    print(f"PHASE 1 PRESENCE BASELINE — {result['snapshot_at']}")
    print(f"Window: last {args.days} days, log: {result['log_path']}")
    print("=" * 60)

    overall = result["overall"]
    print()
    print(f"Total events:          {overall['total_events']}")
    print(f"Unique users:          {overall['unique_users']}")
    print(f"Days with activity:    {overall['days_with_activity']}")
    print(f"Avg daily users:       {overall['avg_daily_active_users']:.2f}")
    print()
    print(f"Per-user (top 15 by total online minutes):")
    for uid, stats in list(result["per_user"].items())[:15]:
        name = stats["display_name"]
        print(f"  {name:30s}  {stats['total_minutes']:8.1f}min  "
              f"{stats['active_days']:3d}d  return_freq={stats['return_freq']:.2f}")

    print()
    print(f"Per-day unique users:")
    for day, users in result["per_day_active_users"].items():
        print(f"  {day}  {len(users):2d} users  ({', '.join(users[:5])}{'...' if len(users) > 5 else ''})")

    delta = result.get("phase0_delta")
    if delta:
        print()
        print("=" * 60)
        print("vs Phase 0 baseline (transcript proxy):")
        print(f"  Phase 0 avg daily active speakers (有發言): {delta['phase0_avg_daily_active_speakers']}")
        print(f"  Phase 1 avg daily active users (進 channel): {delta['phase1_avg_daily_active_users']}")
        print(f"  Delta: {delta['delta_users']:+.2f}")
        print(f"  Note: {delta['note']}")
    print("=" * 60)


if __name__ == "__main__":
    main()

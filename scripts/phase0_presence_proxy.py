"""
phase0_presence_proxy.py — Day 0 Phase 0 baseline (transcript proxy 版本)

⚠️ 重要：這是 PROXY 不是真實 presence。
   - 「active 與否」用「該 speaker 當天有發言」代理
   - 「active 時段」用「該 speaker 當天 first ~ last transcript 時間」代理
   - 靜靜在線聽歌的人 = 不會被算進去（正好打到 P7「presence ≠ engagement」死角）
   - 真實 presence baseline 需要 forward-looking voice_state_update logger（path A）

這份 baseline 的用途：
   - 「啟動 Phase 1 前」的 active engagement baseline（不是 presence）
   - Phase 2 evaluation 時對照看 active speaker 數 / 回流頻率有沒有變化
   - 完全不取代 path A 的 forward-looking presence logger

執行：
   python3 scripts/phase0_presence_proxy.py --days 7 --guild 1133088321254461552
   python3 scripts/phase0_presence_proxy.py --days 30        # 也可以拉更久看趨勢

輸出：
   data/phase0_baseline_{date}.json — summary
   data/phase0_baseline_per_speaker_{date}.json — 每個 speaker 的明細
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from transcript_store import TranscriptStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

GUILD_ID_DEFAULT = 1133088321254461552
OUTPUT_DIR = Path("data")
SKIP_LOG_PATTERNS = ["下一首", "切歌", "換歌", "next song", "skip"]


def main():
    parser = argparse.ArgumentParser(description="Phase 0 presence baseline (transcript proxy)")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--guild", type=int, default=GUILD_ID_DEFAULT)
    parser.add_argument("--db", type=str, default="marvin.db")
    parser.add_argument("--stt-log", type=str, default="stt_history.log",
                        help="STT history log path for skip event counting")
    parser.add_argument("--output-dir", type=str, default=str(OUTPUT_DIR))
    args = parser.parse_args()

    # 1. 拉 transcripts
    store = TranscriptStore(db_path=args.db)
    transcripts = store.get_recent(speaker=None, guild_id=args.guild, days=args.days)
    logger.info(f"拉到 {len(transcripts)} 條 transcripts ({args.days} 天)")

    if not transcripts:
        logger.error("無 transcripts、abort")
        sys.exit(1)

    # 2. 按天 + speaker 切片
    # day_key = YYYY-MM-DD (UTC)
    per_day_per_speaker: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for t in transcripts:
        day_key = datetime.fromtimestamp(t["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d")
        per_day_per_speaker[day_key][t["speaker"]].append(t["timestamp"])

    # 3. 計算 per-speaker stats
    speaker_stats: dict[str, dict] = {}
    all_speakers = set()
    for day_speakers in per_day_per_speaker.values():
        all_speakers.update(day_speakers.keys())

    for speaker in all_speakers:
        active_days = []
        total_active_seconds = 0.0
        total_utterances = 0
        for day_key, day_data in per_day_per_speaker.items():
            ts_list = day_data.get(speaker, [])
            if not ts_list:
                continue
            active_days.append(day_key)
            total_utterances += len(ts_list)
            # active 時段 = first ~ last transcript 該天
            if len(ts_list) >= 2:
                total_active_seconds += max(ts_list) - min(ts_list)

        active_days_count = len(active_days)
        active_minutes_per_day_avg = (
            (total_active_seconds / 60) / active_days_count
            if active_days_count > 0 else 0.0
        )
        speaker_stats[speaker] = {
            "active_days": active_days_count,
            "active_days_ratio": active_days_count / args.days,
            "total_utterances": total_utterances,
            "total_active_minutes": round(total_active_seconds / 60, 1),
            "avg_active_minutes_per_active_day": round(active_minutes_per_day_avg, 1),
            "active_dates": sorted(active_days),
        }

    # 4. 整體統計
    daily_active_counts = [len(d) for d in per_day_per_speaker.values()]
    avg_daily_active_speakers = (
        sum(daily_active_counts) / len(daily_active_counts)
        if daily_active_counts else 0.0
    )

    # 回流頻率 = active_days / total_days
    return_freq = [s["active_days_ratio"] for s in speaker_stats.values()]
    avg_return_freq = sum(return_freq) / len(return_freq) if return_freq else 0.0

    # Top speakers by activity
    sorted_speakers = sorted(
        speaker_stats.items(),
        key=lambda x: -x[1]["total_active_minutes"]
    )

    # 5. Skip event count from stt_history (粗 grep)
    skip_count = 0
    stt_log_path = Path(args.stt_log)
    if stt_log_path.exists():
        try:
            with stt_log_path.open("r", encoding="utf-8", errors="ignore") as f:
                cutoff = time.time() - args.days * 86400
                for line in f:
                    # 簡單 heuristic：含 skip 關鍵字
                    for pat in SKIP_LOG_PATTERNS:
                        if pat in line.lower() or pat in line:
                            skip_count += 1
                            break
        except Exception as e:
            logger.warning(f"讀 STT log 失敗: {e}")
    else:
        logger.warning(f"STT log {args.stt_log} 不存在，skip_count=0")

    # 6. 輸出
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    summary = {
        "params": {
            "days": args.days,
            "guild": args.guild,
            "snapshot_at_utc": datetime.now(timezone.utc).isoformat(),
        },
        "warning": (
            "PROXY METRIC — uses 'speaker has utterance that day' as active proxy. "
            "Real presence (joining voice channel silently) NOT captured. "
            "P7 success metric needs forward-looking voice_state logger."
        ),
        "overall": {
            "total_transcripts": len(transcripts),
            "unique_speakers": len(all_speakers),
            "days_with_activity": len(per_day_per_speaker),
            "avg_daily_active_speakers": round(avg_daily_active_speakers, 2),
            "avg_return_freq_per_speaker": round(avg_return_freq, 3),
            "skip_events_grep_in_stt_log": skip_count,
        },
        "per_day_active_speakers": {
            day: len(per_day_per_speaker[day])
            for day in sorted(per_day_per_speaker.keys())
        },
        "top_speakers_by_active_minutes": [
            {
                "speaker": sp,
                "total_active_minutes": st["total_active_minutes"],
                "active_days": st["active_days"],
                "active_days_ratio": round(st["active_days_ratio"], 3),
                "total_utterances": st["total_utterances"],
            }
            for sp, st in sorted_speakers[:15]
        ],
    }

    summary_path = output_dir / f"phase0_baseline_{date_str}.json"
    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    logger.info(f"寫入 summary: {summary_path}")

    detail_path = output_dir / f"phase0_baseline_per_speaker_{date_str}.json"
    with detail_path.open("w", encoding="utf-8") as f:
        json.dump(speaker_stats, f, ensure_ascii=False, indent=2)
    logger.info(f"寫入 per-speaker 明細: {detail_path}")

    # 7. 印 summary
    print("\n" + "=" * 60)
    print("PHASE 0 BASELINE (TRANSCRIPT PROXY)")
    print("=" * 60)
    print(f"⚠️  PROXY metric — 不含『靜靜在線』的人")
    print()
    print(f"Days analyzed:               {args.days}")
    print(f"Total transcripts:           {len(transcripts)}")
    print(f"Unique speakers:             {len(all_speakers)}")
    print(f"Days with activity:          {len(per_day_per_speaker)}")
    print(f"Avg daily active speakers:   {avg_daily_active_speakers:.2f}")
    print(f"Avg return freq / speaker:   {avg_return_freq:.3f}  ({avg_return_freq*args.days:.1f} / {args.days} days)")
    print(f"Skip events (STT log grep):  {skip_count}")
    print()
    print(f"Top 10 active speakers (by total active minutes):")
    for sp, st in sorted_speakers[:10]:
        print(f"  {sp:30s}  {st['total_active_minutes']:8.1f}min  "
              f"{st['active_days']:2d}d  ({st['total_utterances']} utt)")
    print()
    print(f"Per-day active speaker count:")
    for day in sorted(per_day_per_speaker.keys()):
        print(f"  {day}  {len(per_day_per_speaker[day]):2d} speakers")
    print("=" * 60)


if __name__ == "__main__":
    main()

"""dj_mode_skip_rate.py — 哪種 DJ 口白 mode 講完後大家比較會跳歌？

讀 records/song_plays.jsonl + records/song_skips.jsonl，用 play_id 配對：
一段口白歸屬於它引介的那首歌（見 cogs/music_cog_tail_dj._attach_narration），
mode 為 None 的播放歸到 "baseline" 組（沒口白/沒確定播出）。

用法：
    python scripts/dj_mode_skip_rate.py
    python scripts/dj_mode_skip_rate.py --since 2026-10-01
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAYS_PATH = ROOT / "records" / "song_plays.jsonl"
SKIPS_PATH = ROOT / "records" / "song_skips.jsonl"


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict):
                    rows.append(rec)
    except Exception:
        pass
    return rows


def compute_skip_rates(
    play_rows: list[dict],
    skip_rows: list[dict],
    *,
    window_s: float = 30.0,
    min_n: int = 20,
) -> list[dict]:
    """依 mode 分組算 skip rate：plays=開播數、skips=播出 window_s 秒內被 skip 的次數。"""
    mode_by_play: dict[str, str | None] = {}
    for row in play_rows:
        if row.get("type") != "play":
            continue
        play_id = row.get("play_id")
        if play_id is None:
            continue
        mode_by_play[play_id] = row.get("mode")
    for row in play_rows:
        if row.get("type") != "narration_attach":
            continue
        play_id = row.get("play_id")
        if play_id is None or play_id not in mode_by_play:
            continue
        mode_by_play[play_id] = row.get("mode")

    plays_by_group: dict[str, int] = {}
    for mode in mode_by_play.values():
        group = mode or "baseline"
        plays_by_group[group] = plays_by_group.get(group, 0) + 1

    skipped_play_ids: set = set()
    for row in skip_rows:
        play_id = row.get("play_id")
        if play_id is None or play_id not in mode_by_play:
            continue
        elapsed_s = row.get("elapsed_s")
        if elapsed_s is None or elapsed_s > window_s:
            continue
        skipped_play_ids.add(play_id)

    skips_by_group: dict[str, int] = {}
    for play_id in skipped_play_ids:
        group = mode_by_play[play_id] or "baseline"
        skips_by_group[group] = skips_by_group.get(group, 0) + 1

    results = []
    for group, plays in plays_by_group.items():
        skips = skips_by_group.get(group, 0)
        results.append({
            "group": group,
            "plays": plays,
            "skips": skips,
            "rate": skips / plays,
            "decidable": plays >= min_n,
        })
    results.sort(key=lambda r: r["group"])
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="只算這天之後的 play（YYYY-MM-DD）")
    parser.add_argument("--window-s", type=float, default=30.0)
    parser.add_argument("--min-n", type=int, default=20)
    args = parser.parse_args()

    play_rows = _read_jsonl(PLAYS_PATH)
    skip_rows = _read_jsonl(SKIPS_PATH)

    if args.since:
        since_ts = datetime.strptime(args.since, "%Y-%m-%d").timestamp()  # 本機時區（台灣）的當日 00:00
        play_rows = [r for r in play_rows if (r.get("ts") or 0) >= since_ts]

    results = compute_skip_rates(play_rows, skip_rows, window_s=args.window_s, min_n=args.min_n)

    header = f"{'group':<20}{'plays':>8}{'skips':>8}{'rate':>10}{'decidable':>12}"
    print(header)
    print("-" * len(header))
    for r in results:
        print(f"{r['group']:<20}{r['plays']:>8}{r['skips']:>8}{r['rate']:>10.1%}{str(r['decidable']):>12}")


if __name__ == "__main__":
    main()

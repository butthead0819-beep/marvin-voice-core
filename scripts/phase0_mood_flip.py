"""
phase0_mood_flip.py — Day 0 mood-flip 量測

目的：跑 7 天歷史 transcript，每 60s sample 一次 mood label（透過 Groq llama
分類），計算 mood-flip 比例。結果決定 design doc P4 trigger 設計：

  mood-flip < 5%  → Phase 1 簡化版 15min/round acceptable
  5% - 20%        → 改用每首歌 (5min) trigger + 重估 cost envelope
  > 20%           → 暫停 build, 回 office-hours 重審 P4

執行：
  python scripts/phase0_mood_flip.py --days 7 --guild 1133088321254461552
  python scripts/phase0_mood_flip.py --dry-run             # 只跑前 1 hour 看看
  python scripts/phase0_mood_flip.py --concurrency 5       # 控制 Groq 並發

輸出：data/phase0_mood_flip_{date}.jsonl + summary 印到 stdout

成本估算：~2500 sample × ~500 in tokens × openai/gpt-oss-20b
         ≈ $0.06 USD（不用擔心）

⚠️ MOOD CLASSIFIER PROMPT 在下面 MOOD_CLASSIFIER_SYSTEM/USER 兩個常數，
   review/迭代直接改那兩段。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# 確保 import root-level modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # type: ignore
from groq import AsyncGroq  # type: ignore

from transcript_store import TranscriptStore

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── 可調參數 ─────────────────────────────────────────────────────────────────

GUILD_ID_DEFAULT = 1133088321254461552      # marvin.db 主要 guild
SAMPLE_INTERVAL_S = 60                       # 每 N 秒 sample 一次
WINDOW_S = 5 * 60                            # 每 sample 看過去 N 秒對話
MIN_TRANSCRIPTS_PER_WINDOW = 2               # < 此數視為「無對話」、不分類

# Groq
MODEL = "openai/gpt-oss-20b"
MAX_TOKENS = 10                              # mood 是單字、output 短
TIMEOUT = 10.0
DEFAULT_CONCURRENCY = 3                      # 並發數 (Groq free tier rate limit 緊、3 比較穩)
RETRY_MAX = 4                                # 失敗 retry 次數 (429 backoff 用得到)
RETRY_BASE_S = 3                             # retry backoff 起始秒數 (exponential)

# Output
OUTPUT_DIR = Path("data")
MOOD_LABELS = ("放鬆", "興奮", "低落", "分歧")
NO_CONVO_LABEL = "無對話"
FAIL_LABEL = "分類失敗"

# ── MOOD CLASSIFIER PROMPT（REVIEW HERE） ─────────────────────────────────────
# v1 設計：4 檔 mood，sourced from /office-hours subagent 提案。
# 「分歧」這檔是 Group Mood Arbitrage 的入口訊號——多人情緒不一致時的標記。

MOOD_CLASSIFIER_SYSTEM = """你是房間 vibe 分類器。任務：讀一段 Discord 多人對話片段，分類到一個 mood label。

4 種 mood:
- 放鬆：緩和閒聊、無明顯情緒起伏、日常話題
- 興奮：笑、驚訝、好玩、節奏快、互相吐槽熱絡
- 低落：抱怨、累、煩躁、話少、低能量
- 分歧：多人情緒不一致、爭論、有人嗨有人冷、話不投機

只輸出一個詞 (放鬆/興奮/低落/分歧)，不要其他文字、不要標點。"""

def build_user_prompt(transcripts: list[dict]) -> str:
    """組 user prompt。transcripts 是 [{speaker, text, timestamp}] 已時序排好。"""
    lines = []
    for t in transcripts:
        lines.append(f"{t['speaker']}: {t['text']}")
    return "對話片段（5 分鐘窗口）：\n" + "\n".join(lines)

# ── 主流程 ───────────────────────────────────────────────────────────────────

async def classify_mood(
    client: AsyncGroq,
    transcripts: list[dict],
    sem: asyncio.Semaphore,
) -> str:
    """送一個 5min 窗口給 Groq、回 mood label。"""
    if len(transcripts) < MIN_TRANSCRIPTS_PER_WINDOW:
        return NO_CONVO_LABEL

    user_prompt = build_user_prompt(transcripts)

    async with sem:
        for attempt in range(RETRY_MAX + 1):
            try:
                resp = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=MODEL,
                        messages=[
                            {"role": "system", "content": MOOD_CLASSIFIER_SYSTEM},
                            {"role": "user", "content": user_prompt},
                        ],
                        temperature=0.2,            # 分類任務、低溫
                        max_tokens=MAX_TOKENS,
                        stream=False,
                    ),
                    timeout=TIMEOUT,
                )
                content = resp.choices[0].message.content.strip()
                # 容錯解析：只取第一個出現的 mood label
                for label in MOOD_LABELS:
                    if label in content:
                        return label
                logger.warning(f"[classifier] 無法解析: {content!r}")
                return FAIL_LABEL
            except Exception as e:
                if attempt < RETRY_MAX:
                    # Exponential backoff: 3, 6, 12, 24 s — 給 Groq rate limit 喘息空間
                    await asyncio.sleep(RETRY_BASE_S * (2 ** attempt))
                    continue
                logger.warning(f"[classifier] 失敗 (attempt {attempt+1}): {e}")
                return FAIL_LABEL
    return FAIL_LABEL


def build_sample_windows(
    transcripts: list[dict],
    window_s: int,
    interval_s: int,
) -> list[tuple[float, list[dict]]]:
    """
    從時序 transcripts 切出 sliding sample windows。

    回 [(sample_ts, transcripts_in_window), ...]
    sample_ts 是 window 的右端（最新時間點）。
    """
    if not transcripts:
        return []

    # 排序確認時序
    transcripts = sorted(transcripts, key=lambda x: x["timestamp"])
    start_ts = transcripts[0]["timestamp"]
    end_ts = transcripts[-1]["timestamp"]

    samples = []
    cur_ts = start_ts + window_s   # 第一個 sample 至少要有滿一個 window
    idx_start = 0

    while cur_ts <= end_ts:
        window_left = cur_ts - window_s
        # 滑 idx_start
        while idx_start < len(transcripts) and transcripts[idx_start]["timestamp"] < window_left:
            idx_start += 1
        # 收集 [window_left, cur_ts] 內的
        window_items = []
        for t in transcripts[idx_start:]:
            if t["timestamp"] > cur_ts:
                break
            window_items.append(t)
        samples.append((cur_ts, window_items))
        cur_ts += interval_s

    return samples


def compute_flip_stats(labels: list[str]) -> dict:
    """
    從 sample labels 算 flip 統計。
    跨 NO_CONVO 不算 flip（NO_CONVO 是「沒對話」、不是 mood 變化）。
    """
    distribution = Counter(labels)
    total = len(labels)

    # Flip 計算：只看連續兩個都是 valid mood 的 transitions
    valid_transitions = 0
    flips = 0
    for prev, curr in zip(labels, labels[1:]):
        if prev in MOOD_LABELS and curr in MOOD_LABELS:
            valid_transitions += 1
            if prev != curr:
                flips += 1

    flip_rate = (flips / valid_transitions) if valid_transitions > 0 else 0.0

    return {
        "total_samples": total,
        "distribution": dict(distribution),
        "valid_transitions": valid_transitions,
        "flips": flips,
        "flip_rate": flip_rate,
        "judgement_band": classify_band(flip_rate),
    }


def classify_band(flip_rate: float) -> str:
    """三 band 判讀 (per design doc Phase 0.5 gate)。"""
    if flip_rate < 0.05:
        return "<5%  → Phase 1 簡化版 15min/round acceptable"
    if flip_rate <= 0.20:
        return "5-20% → 改用 5min trigger + 重估 cost envelope"
    return ">20% → 暫停 build, 重審 P4"


async def main():
    parser = argparse.ArgumentParser(description="Phase 0 mood-flip measurement")
    parser.add_argument("--days", type=int, default=7, help="取多少天 transcripts")
    parser.add_argument("--guild", type=int, default=GUILD_ID_DEFAULT)
    parser.add_argument("--db", type=str, default="marvin.db")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--dry-run", action="store_true", help="只跑前 1 hour sample")
    parser.add_argument("--output-dir", type=str, default=str(OUTPUT_DIR))
    args = parser.parse_args()

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        logger.error("GROQ_API_KEY 未設置，abort")
        sys.exit(1)

    # 1. 拉 transcripts
    store = TranscriptStore(db_path=args.db)
    transcripts = store.get_recent(speaker=None, guild_id=args.guild, days=args.days)
    logger.info(f"拉到 {len(transcripts)} 條 transcripts (guild={args.guild}, days={args.days})")

    if not transcripts:
        logger.error("無 transcripts，abort")
        sys.exit(1)

    # 2. 切 sample windows
    samples = build_sample_windows(transcripts, WINDOW_S, SAMPLE_INTERVAL_S)
    logger.info(f"切出 {len(samples)} 個 sample windows ({SAMPLE_INTERVAL_S}s 間隔, {WINDOW_S}s 窗)")

    if args.dry_run:
        # dry-run 只跑前 1 hour = 60 個 sample
        samples = samples[:60]
        logger.info(f"DRY-RUN: 截取前 {len(samples)} 個 sample")

    # 3. 並發跑 classifier
    client = AsyncGroq(api_key=api_key)
    sem = asyncio.Semaphore(args.concurrency)
    t0 = time.time()

    async def classify_one(idx: int, sample_ts: float, window: list[dict]):
        label = await classify_mood(client, window, sem)
        if (idx + 1) % 50 == 0:
            elapsed = time.time() - t0
            rate = (idx + 1) / elapsed if elapsed > 0 else 0
            eta = (len(samples) - idx - 1) / rate if rate > 0 else 0
            logger.info(f"  進度 {idx+1}/{len(samples)} ({rate:.1f}/s, ETA {eta:.0f}s)")
        return (sample_ts, label, len(window))

    tasks = [classify_one(i, ts, w) for i, (ts, w) in enumerate(samples)]
    results = await asyncio.gather(*tasks)

    elapsed = time.time() - t0
    logger.info(f"分類完成 {len(results)} sample，耗時 {elapsed:.1f}s")

    # 4. 寫 JSONL
    OUTPUT_DIR_P = Path(args.output_dir)
    OUTPUT_DIR_P.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_path = OUTPUT_DIR_P / f"phase0_mood_flip_{date_str}.jsonl"

    with output_path.open("w", encoding="utf-8") as f:
        for sample_ts, label, n_txt in results:
            iso_ts = datetime.fromtimestamp(sample_ts, tz=timezone.utc).isoformat()
            f.write(json.dumps({
                "ts": sample_ts,
                "iso_ts": iso_ts,
                "mood": label,
                "transcripts_in_window": n_txt,
            }, ensure_ascii=False) + "\n")
    logger.info(f"寫入 {output_path}")

    # 5. 統計
    labels = [r[1] for r in results]
    stats = compute_flip_stats(labels)

    print("\n" + "=" * 60)
    print("PHASE 0 MOOD-FLIP RESULT")
    print("=" * 60)
    print(f"Total samples:     {stats['total_samples']}")
    print(f"Distribution:")
    for label, count in sorted(stats["distribution"].items(), key=lambda x: -x[1]):
        pct = count / stats["total_samples"] * 100
        print(f"  {label:8s}  {count:5d}  ({pct:5.1f}%)")
    print(f"Valid transitions: {stats['valid_transitions']}")
    print(f"Mood flips:        {stats['flips']}")
    print(f"Flip rate:         {stats['flip_rate']:.3f} ({stats['flip_rate']*100:.1f}%)")
    print(f"\nJudgement: {stats['judgement_band']}")
    print("=" * 60)

    # 6. 寫 summary
    summary_path = OUTPUT_DIR_P / f"phase0_mood_flip_{date_str}_summary.json"
    with summary_path.open("w", encoding="utf-8") as f:
        json.dump({
            "params": {
                "days": args.days,
                "guild": args.guild,
                "sample_interval_s": SAMPLE_INTERVAL_S,
                "window_s": WINDOW_S,
                "model": MODEL,
                "dry_run": args.dry_run,
            },
            "stats": stats,
            "elapsed_s": elapsed,
            "sample_jsonl": str(output_path),
        }, f, ensure_ascii=False, indent=2)
    logger.info(f"寫入 {summary_path}")


if __name__ == "__main__":
    asyncio.run(main())

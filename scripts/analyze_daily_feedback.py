#!/usr/bin/env python3
"""Run offline feedback analysis for one date.

Pipeline:
  records/agent_recommendations.jsonl (filtered to date)
  → transcript_store window fetch [rec.ts, rec.ts + feedback_window_s]
  → analyzers[rec.agent].analyze() (LLM)
  → TieredFeedbackWriter.write() (T1: music_memory)
  → TieredFeedbackWriter.emit_audit_lines() → records/audit_<date>.md
  → records/feedback_analysis_<date>.md (L1 summary report)

Usage:
    python scripts/analyze_daily_feedback.py 2026-05-19
    python scripts/analyze_daily_feedback.py 2026-05-19 --dry-run
    python scripts/analyze_daily_feedback.py 2026-05-19 --recs-path /tmp/x.jsonl

Env:
    GROQ_API_KEY — required for LLM call (skip with --dry-run)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from intent_agents.feedback_analyzer import (  # noqa: E402
    FeedbackAnalyzer, FeedbackResult, MusicFeedbackAnalyzer, Utterance,
)
from intent_agents.feedback_batch import NightlyFeedbackBatch  # noqa: E402
from intent_agents.recommendation import DEFAULT_LOG_PATH, Recommendation  # noqa: E402
from intent_agents.tiered_feedback_writer import TieredFeedbackWriter  # noqa: E402

logger = logging.getLogger(__name__)


def make_transcript_fetcher(transcript_store, guild_id: int = 0):
    """Wrap TranscriptStore.get_recent into a window fetcher.

    get_recent is now-anchored (uses time.time() - cutoff), so we fetch back
    far enough to cover start_ts then filter to [start_ts, end_ts] in Python.
    Offline batch — extra rows don't matter for cost.
    """
    def _fetch(speaker: str, start_ts: float, end_ts: float) -> list[Utterance]:
        days_back = max(1, int((time.time() - start_ts) / 86400) + 2)
        try:
            rows = transcript_store.get_recent(
                speaker=speaker, guild_id=guild_id, days=days_back,
            )
        except Exception as e:
            logger.warning(f"⚠️ [fetcher] get_recent 失敗 (speaker={speaker}): {e}")
            return []
        return [
            Utterance(speaker=r["speaker"], text=r["text"], timestamp=r["timestamp"])
            for r in rows
            if start_ts <= r["timestamp"] <= end_ts
        ]
    return _fetch


def render_analysis_report(
    date_str: str,
    results: list[tuple[Recommendation, FeedbackResult]],
) -> str:
    """L1 summary: per-rec attribution. 給人類審視也供未來 trend audit 用。"""
    if not results:
        return f"# Feedback Analysis — {date_str}\n\nNo recommendations to process.\n"

    sentiments = Counter(r.sentiment for _, r in results)
    agents = Counter(rec.agent for rec, _ in results)

    lines = [
        f"# Feedback Analysis — {date_str}",
        "",
        f"Total recommendations analyzed: **{len(results)}**",
        "",
        "## Sentiment breakdown",
        "",
    ]
    for s in ("positive", "negative", "neutral", "skipped_immediately"):
        if sentiments.get(s):
            lines.append(f"- {s}: {sentiments[s]}")
    lines.append("")
    lines.append("## Agent breakdown")
    lines.append("")
    for agent, count in agents.most_common():
        lines.append(f"- {agent}: {count}")
    lines.append("")
    lines.append("## Per-recommendation detail")
    lines.append("")
    for rec, result in results:
        ts_iso = time.strftime("%H:%M:%S", time.localtime(rec.ts))
        lines.append(
            f"- {ts_iso} [{rec.agent}] {rec.speaker} → {rec.selected} "
            f"⇒ **{result.sentiment}** (conf {result.confidence:.2f}) — {result.reason}"
        )
    return "\n".join(lines) + "\n"


def render_audit_report(date_str: str, audit_lines: list[str]) -> str:
    """T3 audit: anomalies needing human review. ALWAYS read-only."""
    if not audit_lines:
        return (
            f"# Audit — {date_str}\n\n"
            "No anomalies. All recommendations cleanly classified.\n"
        )
    return (
        f"# Audit — {date_str}\n\n"
        f"**{len(audit_lines)}** recommendations need human review. "
        "Do NOT auto-process — this report is read-only.\n\n"
        + "\n".join(audit_lines)
        + "\n"
    )


async def run(
    date_str: str,
    *,
    recs_path: Path = Path(DEFAULT_LOG_PATH),
    output_dir: Path = Path("records"),
    music_memory=None,
    suki_memory=None,
    transcript_store=None,
    llm_client=None,
    analyzers: dict[str, FeedbackAnalyzer] | None = None,
    dry_run: bool = False,
) -> dict:
    """Run the full pipeline for one date. Returns summary dict."""
    # Lazy import prod stores so tests can skip them
    if music_memory is None:
        from music_memory import MusicMemory
        music_memory = MusicMemory()
    if transcript_store is None:
        from transcript_store import TranscriptStore
        transcript_store = TranscriptStore()
    # suki_memory 為 optional: 不啟用 T2 可傳 None
    if suki_memory is None:
        try:
            from suki_memory import MemoryManager
            suki_memory = MemoryManager()
        except Exception as e:
            logger.warning(f"⚠️ [analyze] suki_memory 載入失敗，T2 跳過: {e}")
            suki_memory = None

    if analyzers is None:
        if llm_client is None:
            # Real run needs an LLM. Use Groq (already in deps).
            try:
                from groq import AsyncGroq
            except ImportError as e:
                raise RuntimeError(
                    "groq SDK not installed; pip install groq"
                ) from e
            api_key = os.environ.get("GROQ_API_KEY")
            if not api_key:
                raise RuntimeError("GROQ_API_KEY not set; required for analyzer LLM")
            llm_client = AsyncGroq(api_key=api_key)
        analyzers = {"music": MusicFeedbackAnalyzer(llm_client=llm_client)}

    fetcher = make_transcript_fetcher(transcript_store)
    batch = NightlyFeedbackBatch(
        analyzers=analyzers,
        transcript_fetcher=fetcher,
        recommendations_path=recs_path,
    )
    results = await batch.run_for_date(date_str)
    logger.info(f"[analyze] {date_str}: {len(results)} recs analyzed")

    output_dir.mkdir(parents=True, exist_ok=True)
    analysis_path = output_dir / f"feedback_analysis_{date_str}.md"
    audit_path = output_dir / f"audit_{date_str}.md"

    writer = TieredFeedbackWriter(
        music_memory=music_memory, suki_memory=suki_memory,
    )
    audit_lines = writer.emit_audit_lines(results)

    promotions: list[dict] = []
    if not dry_run:
        writer.write(results)                                   # T1
        promotions = writer.apply_t2_promotions(results)        # T2 (post-T1)

    analysis_path.write_text(render_analysis_report(date_str, results), encoding="utf-8")
    audit_path.write_text(render_audit_report(date_str, audit_lines), encoding="utf-8")

    return {
        "date": date_str,
        "total": len(results),
        "audit_lines": len(audit_lines),
        "t2_promotions": len(promotions),
        "analysis_path": str(analysis_path),
        "audit_path": str(audit_path),
        "dry_run": dry_run,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline feedback analysis batch")
    parser.add_argument("date", help="YYYY-MM-DD (local)")
    parser.add_argument(
        "--recs-path", type=Path, default=Path(DEFAULT_LOG_PATH),
        help="Path to recommendations.jsonl",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("records"),
        help="Where to write report files",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Render reports but skip T1 store writes",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    summary = asyncio.run(run(
        args.date,
        recs_path=args.recs_path,
        output_dir=args.output_dir,
        dry_run=args.dry_run,
    ))
    print(f"\n[analyze] done: {summary}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

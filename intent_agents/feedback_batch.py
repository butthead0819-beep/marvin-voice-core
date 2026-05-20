"""NightlyFeedbackBatch — orchestrates per-rec feedback analysis.

Per `feedback_meta_agent_taxonomy.md`：driver/orchestrator，**不是** agent。
讀 records/agent_recommendations.jsonl → 對每筆 rec 抓 speaker 在
[rec.ts, rec.ts + feedback_window_s] 的 utt 窗口 → 派給 analyzers[rec.agent]
→ 回傳 (rec, FeedbackResult) tuples 供下一層 tiered writer 處理。

Orchestrator does NOT:
- Write back to any store（那是 T1/T2/T3 tiered writer 的事）
- 自動觸發任何 store mutation
- 介入 routing / bid 流程

Failure isolation：單一 rec 處理失敗（analyzer 炸 / fetcher 炸）→ 該 rec skip，
其他繼續。整批不該因一筆壞掉而中斷。
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from intent_agents.feedback_analyzer import FeedbackAnalyzer, FeedbackResult, Utterance
from intent_agents.recommendation import (
    DEFAULT_LOG_PATH,
    Recommendation,
    read_recommendations,
)

logger = logging.getLogger(__name__)

# Window fetcher signature: (speaker, start_ts, end_ts) -> list[Utterance]
TranscriptFetcher = Callable[[str, float, float], list[Utterance]]


class NightlyFeedbackBatch:
    """Drives offline feedback analysis for one date's worth of recommendations."""

    def __init__(
        self,
        analyzers: dict[str, FeedbackAnalyzer],
        transcript_fetcher: TranscriptFetcher,
        recommendations_path: Path | str = DEFAULT_LOG_PATH,
    ):
        self.analyzers = analyzers
        self.fetch_utts = transcript_fetcher
        self.recommendations_path = Path(recommendations_path)

    async def run_for_date(
        self, date_str: str,
    ) -> list[tuple[Recommendation, FeedbackResult]]:
        """Process all recommendations whose ts falls on local date_str.

        Returns list of (rec, result) in jsonl encounter order. Caller passes
        these to T1/T2/T3 tiered writer for store updates.
        """
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError as e:
            logger.error(f"❌ [FeedbackBatch] date_str 格式錯誤: {e}")
            return []

        out: list[tuple[Recommendation, FeedbackResult]] = []
        for rec in read_recommendations(path=self.recommendations_path):
            # Date gate（local date 比對）
            try:
                rec_date = datetime.fromtimestamp(rec.ts).date()
            except (OverflowError, OSError, ValueError):
                continue
            if rec_date != target_date:
                continue

            # Analyzer dispatch
            analyzer = self.analyzers.get(rec.agent)
            if analyzer is None:
                logger.info(
                    f"ℹ️ [FeedbackBatch] 無 analyzer for agent={rec.agent}（rec ts={rec.ts}），略過"
                )
                continue

            # Window fetch
            try:
                utts = self.fetch_utts(
                    rec.speaker, rec.ts, rec.ts + rec.feedback_window_s,
                )
            except Exception as e:
                logger.warning(
                    f"⚠️ [FeedbackBatch] transcript fetch 失敗 (speaker={rec.speaker}, "
                    f"ts={rec.ts}): {e}，略過此 rec"
                )
                continue

            # Analyze
            try:
                result = await analyzer.analyze(rec, utts)
            except Exception as e:
                logger.warning(
                    f"⚠️ [FeedbackBatch] analyzer.analyze 失敗 (agent={rec.agent}, "
                    f"selected={rec.selected}): {e}，略過此 rec"
                )
                continue

            out.append((rec, result))

        return out

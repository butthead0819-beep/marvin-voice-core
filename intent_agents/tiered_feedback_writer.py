"""TieredFeedbackWriter — 把 NightlyFeedbackBatch 產出按 T1/T2/T3 寫回 store。

Per `feedback_slow_learning_via_recommendations.md` Section 3a：
- T1: music_memory.add_recommendation_feedback — 全自動（這個 module 做）
- T2: suki.likes/dislikes — threshold 後自動（**暫緩**，下次 ticket 做 history
  query + artist extraction）
- T3: audit_<date>.md 行 — 永遠 read-only，給人類審視（這個 module emit lines）

Sentiment → music_memory result mapping:
- positive → "liked"
- negative / skipped_immediately → "skipped"
- neutral / unknown → None（不寫，避免污染）

T1 confidence threshold（default 0.5）：低於則跳 T1 寫入，但仍進 T3 audit。
T3 觸發條件：confidence < t3_audit_threshold（default 0.5）或 reason 含 error。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from intent_agents.feedback_analyzer import FeedbackResult
from intent_agents.recommendation import Recommendation

logger = logging.getLogger(__name__)


# ── Sentiment mapping ─────────────────────────────────────────────────────

_SENTIMENT_TO_MUSIC_RESULT = {
    "positive": "liked",
    "negative": "skipped",
    "skipped_immediately": "skipped",
    # "neutral" → None (no write)
}


def sentiment_to_music_result(sentiment: str) -> Optional[str]:
    """Map analyzer sentiment to music_memory result string. None = don't write."""
    return _SENTIMENT_TO_MUSIC_RESULT.get(sentiment)


# ── Writer ─────────────────────────────────────────────────────────────────

class TieredFeedbackWriter:
    """Apply T1/T3 rules to NightlyFeedbackBatch results."""

    def __init__(
        self,
        music_memory: Any,
        t1_min_confidence: float = 0.5,
        t3_audit_threshold: float = 0.5,
    ):
        self.music_memory = music_memory
        self.t1_min_confidence = t1_min_confidence
        self.t3_audit_threshold = t3_audit_threshold

    # ── T1 ────────────────────────────────────────────────────────────────

    def write(
        self,
        results: list[tuple[Recommendation, FeedbackResult]],
    ) -> None:
        """Apply T1 writes. Per-result exception isolated."""
        for rec, result in results:
            try:
                self._t1_write_one(rec, result)
            except Exception as e:
                logger.warning(
                    f"⚠️ [TieredWriter] T1 寫入失敗 (rec={rec.selected}, "
                    f"speaker={rec.speaker}): {e}，繼續下一筆"
                )

    def _t1_write_one(self, rec: Recommendation, result: FeedbackResult) -> None:
        # confidence gate
        if result.confidence < self.t1_min_confidence:
            return
        # sentiment → music_memory result mapping
        music_result = sentiment_to_music_result(result.sentiment)
        if music_result is None:
            return
        # agent-type gate（music_memory 只接 music 的 feedback）
        if rec.agent != "music":
            return
        self.music_memory.add_recommendation_feedback(
            rec.speaker, rec.selected, music_result,
        )

    # ── T3 ────────────────────────────────────────────────────────────────

    def emit_audit_lines(
        self,
        results: list[tuple[Recommendation, FeedbackResult]],
    ) -> list[str]:
        """Produce markdown bullet lines for audit_<date>.md.

        Only emits for anomalies:
        - confidence < t3_audit_threshold
        - reason contains 'error' / '失敗'
        - sentiment is 'neutral' but confidence 0.0（LLM 失敗的 marker）
        """
        lines: list[str] = []
        for rec, result in results:
            reason = result.reason.lower()
            has_error = "error" in reason or "失敗" in result.reason or "錯誤" in result.reason
            low_conf = result.confidence < self.t3_audit_threshold

            if not (has_error or low_conf):
                continue

            tag_parts: list[str] = []
            if low_conf:
                tag_parts.append(f"low_confidence={result.confidence:.2f}")
            if has_error:
                tag_parts.append("llm_error")
            tag = ", ".join(tag_parts)

            evidence_str = ""
            if result.evidence:
                evidence_str = "（evidence: " + " / ".join(result.evidence) + "）"

            line = (
                f"- [{tag}] speaker={rec.speaker} agent={rec.agent} "
                f"selected={rec.selected} sentiment={result.sentiment} "
                f"reason={result.reason}{evidence_str}"
            )
            lines.append(line)
        return lines

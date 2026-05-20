"""TieredFeedbackWriter — 把 NightlyFeedbackBatch 產出按 T1/T2/T3 寫回 store。

Per `feedback_slow_learning_via_recommendations.md` Section 3a：
- T1: music_memory.add_recommendation_feedback — 全自動（per-rec）
- T2: suki.likes/dislikes — threshold 後自動（≥3 same direction in 30d window）
- T3: audit_<date>.md 行 — 永遠 read-only，給人類審視（emit lines）

Sentiment → music_memory result mapping:
- positive → "liked"
- negative / skipped_immediately → "skipped"
- neutral / unknown → None（不寫，避免污染）

T1 confidence threshold（default 0.5）：低於則跳 T1 寫入，但仍進 T3 audit。
T2 threshold：在 T1 寫入後，查 music_memory recent feedback 計數，達 ≥3 同向才推進 suki。
T3 觸發條件：confidence < t3_audit_threshold（default 0.5）或 reason 含 error。
"""
from __future__ import annotations

import logging
import time
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
    """Apply T1/T2/T3 rules to NightlyFeedbackBatch results."""

    T2_DEFAULT_THRESHOLD = 3       # 連續 N 次同向才推進 suki
    T2_DEFAULT_WINDOW_DAYS = 30    # 近 N 天內才算

    def __init__(
        self,
        music_memory: Any,
        suki_memory: Optional[Any] = None,
        *,
        t1_min_confidence: float = 0.5,
        t2_threshold: int = T2_DEFAULT_THRESHOLD,
        t2_window_days: int = T2_DEFAULT_WINDOW_DAYS,
        t3_audit_threshold: float = 0.5,
        clock: callable = time.time,
    ):
        self.music_memory = music_memory
        self.suki_memory = suki_memory  # None = T2 skipped silently（CLI 可選擇不啟用）
        self.t1_min_confidence = t1_min_confidence
        self.t2_threshold = t2_threshold
        self.t2_window_days = t2_window_days
        self.t3_audit_threshold = t3_audit_threshold
        self.clock = clock

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

    # ── T2 ────────────────────────────────────────────────────────────────

    def apply_t2_promotions(
        self,
        results: list[tuple[Recommendation, FeedbackResult]],
    ) -> list[dict]:
        """For each result：count music_memory recent feedback (post-T1) → if
        ≥ threshold same direction within window → promote to suki likes/dislikes.

        Returns list of promotion dicts for audit/inspection: [{speaker, item, direction, count}].
        Must be called AFTER write() so T1's latest feedback is in the count.

        Suki has set-merge semantics on likes/dislikes (suki_memory.update_player_memory),
        so duplicate promotions are idempotent — safe to call multiple times.
        """
        if self.suki_memory is None:
            return []
        promotions: list[dict] = []
        since = self.clock() - self.t2_window_days * 86400
        for rec, result in results:
            if rec.agent != "music":
                continue
            if result.confidence < self.t1_min_confidence:
                continue  # 低信心不參與 T2 計數（T1 也沒寫，不會有矛盾）
            direction = self._sentiment_to_direction(result.sentiment)
            if direction is None:
                continue
            try:
                promo = self._promote_if_threshold_met(
                    rec.speaker, rec.selected, direction, since,
                )
            except Exception as e:
                logger.warning(
                    f"⚠️ [TieredWriter T2] promotion 失敗 (speaker={rec.speaker}, "
                    f"item={rec.selected}): {e}，繼續下一筆"
                )
                continue
            if promo is not None:
                promotions.append(promo)
        return promotions

    @staticmethod
    def _sentiment_to_direction(sentiment: str) -> Optional[str]:
        if sentiment == "positive":
            return "liked"
        if sentiment in ("negative", "skipped_immediately"):
            return "skipped"
        return None

    def _promote_if_threshold_met(
        self, speaker: str, item: str, direction: str, since: float,
    ) -> Optional[dict]:
        # Speaker must exist in suki — never auto-create players from feedback
        if not self.suki_memory.has_player(speaker):
            return None
        history = self.music_memory.get_recent_feedback(speaker, since)
        count = sum(
            1 for e in history
            if e.get("title") == item and e.get("result") == direction
        )
        if count < self.t2_threshold:
            return None
        suki_field = "likes" if direction == "liked" else "dislikes"
        self.suki_memory.update_player_memory(speaker, {suki_field: [item]})
        return {
            "speaker": speaker,
            "item": item,
            "direction": direction,
            "count": count,
            "suki_field": suki_field,
        }

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

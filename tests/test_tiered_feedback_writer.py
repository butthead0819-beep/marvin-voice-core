"""TDD：T1/T3 Tiered Feedback Writer.

Per `feedback_slow_learning_via_recommendations.md` Section 3a：
- **T1: 結構化證據** → music_memory.add_recommendation_feedback（全自動）
- **T2: 聚合偏好** → suki.likes/dislikes（threshold 後自動）— **暫緩，下次 ticket**
- **T3: 身份／印象** → audit_<date>.md（永遠 read-only，給人類審）

今天 scope：T1 + T3。T2 threshold + history query + artist extraction 設計複雜，
另開一輪做。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from intent_agents.feedback_analyzer import FeedbackResult
from intent_agents.recommendation import Recommendation
from intent_agents.tiered_feedback_writer import (
    TieredFeedbackWriter,
    sentiment_to_music_result,
)


def _rec(speaker: str = "大肚", selected: str = "周杰倫 夜曲") -> Recommendation:
    return Recommendation(
        ts=1000.0, agent="music", speaker=speaker, trigger="queue_empty",
        selected=selected, reason_internal="r", explanation_uttered="e",
        feedback_window_s=300, channel_state={},
    )


def _result(sentiment: str, confidence: float = 0.8,
            reason: str = "mock") -> FeedbackResult:
    return FeedbackResult(
        sentiment=sentiment, confidence=confidence,
        reason=reason, evidence=(),
    )


# ── 1. Sentiment → music_memory result mapping ────────────────────────────

def test_sentiment_mapping_positive_to_liked():
    assert sentiment_to_music_result("positive") == "liked"


def test_sentiment_mapping_negative_to_skipped():
    assert sentiment_to_music_result("negative") == "skipped"


def test_sentiment_mapping_skipped_immediately_to_skipped():
    assert sentiment_to_music_result("skipped_immediately") == "skipped"


def test_sentiment_mapping_neutral_returns_none():
    """neutral 不該寫進 store — 沒有有效訊號。"""
    assert sentiment_to_music_result("neutral") is None


def test_sentiment_mapping_unknown_returns_none():
    assert sentiment_to_music_result("ecstatic") is None


# ── 2. T1: music_memory write ─────────────────────────────────────────────

def test_t1_positive_writes_liked_to_music_memory():
    music_memory = MagicMock()
    writer = TieredFeedbackWriter(music_memory=music_memory)

    writer.write([(_rec(), _result("positive"))])

    music_memory.add_recommendation_feedback.assert_called_once_with(
        "大肚", "周杰倫 夜曲", "liked",
    )


def test_t1_negative_writes_skipped_to_music_memory():
    music_memory = MagicMock()
    writer = TieredFeedbackWriter(music_memory=music_memory)

    writer.write([(_rec(), _result("negative"))])

    music_memory.add_recommendation_feedback.assert_called_once_with(
        "大肚", "周杰倫 夜曲", "skipped",
    )


def test_t1_neutral_does_not_write():
    """neutral 訊號弱 → 不污染 store。"""
    music_memory = MagicMock()
    writer = TieredFeedbackWriter(music_memory=music_memory)

    writer.write([(_rec(), _result("neutral"))])

    music_memory.add_recommendation_feedback.assert_not_called()


def test_t1_low_confidence_does_not_write():
    """confidence < threshold → 不該污染 store；只進 audit。"""
    music_memory = MagicMock()
    writer = TieredFeedbackWriter(
        music_memory=music_memory, t1_min_confidence=0.5,
    )
    writer.write([(_rec(), _result("positive", confidence=0.3))])

    music_memory.add_recommendation_feedback.assert_not_called()


def test_t1_at_threshold_does_write():
    """confidence == threshold 邊界 → 寫入（包含）。"""
    music_memory = MagicMock()
    writer = TieredFeedbackWriter(
        music_memory=music_memory, t1_min_confidence=0.5,
    )
    writer.write([(_rec(), _result("positive", confidence=0.5))])

    music_memory.add_recommendation_feedback.assert_called_once()


def test_t1_non_music_agent_skipped_for_music_memory():
    """rec.agent != 'music' 不該寫進 music_memory（其他 agent 各自 store）。"""
    music_memory = MagicMock()
    writer = TieredFeedbackWriter(music_memory=music_memory)

    topic_rec = Recommendation(
        ts=1000.0, agent="topic", speaker="大肚", trigger="t",
        selected="天氣很好", reason_internal="r", explanation_uttered="e",
        feedback_window_s=120, channel_state={},
    )
    writer.write([(topic_rec, _result("positive"))])

    music_memory.add_recommendation_feedback.assert_not_called()


# ── 3. T3: audit report emission ──────────────────────────────────────────

def test_t3_low_confidence_emits_audit_line():
    writer = TieredFeedbackWriter(music_memory=MagicMock())
    lines = writer.emit_audit_lines([(_rec(), _result("positive", confidence=0.2))])

    assert len(lines) == 1
    assert "low_confidence" in lines[0].lower() or "低信心" in lines[0]
    assert "大肚" in lines[0]
    assert "周杰倫 夜曲" in lines[0]


def test_t3_zero_confidence_llm_error_emits_audit_line():
    writer = TieredFeedbackWriter(music_memory=MagicMock())
    result = _result("neutral", confidence=0.0, reason="llm_error: timeout")
    lines = writer.emit_audit_lines([(_rec(), result)])

    assert len(lines) == 1
    assert "llm_error" in lines[0] or "錯誤" in lines[0]


def test_t3_high_confidence_clean_result_no_audit_line():
    """高信心、無異常 → 不該出現在 audit 裡（不是給人類看的）。"""
    writer = TieredFeedbackWriter(music_memory=MagicMock())
    lines = writer.emit_audit_lines([(_rec(), _result("positive", confidence=0.9))])

    assert lines == []


def test_t3_audit_line_format_includes_evidence():
    """audit 行為 markdown bullet，含證據連結方便 review。"""
    writer = TieredFeedbackWriter(music_memory=MagicMock())
    r = FeedbackResult(
        sentiment="negative", confidence=0.4,
        reason="user 抗議但訊號弱",
        evidence=("換一首啦",),
    )
    lines = writer.emit_audit_lines([(_rec(), r)])

    assert len(lines) == 1
    line = lines[0]
    assert line.startswith("- "), "audit 行應該是 markdown bullet"
    assert "換一首啦" in line


# ── 4. Per-result failure isolation ───────────────────────────────────────

def test_t1_write_exception_isolated():
    """music_memory.add_recommendation_feedback 炸 → 跳該筆，其他繼續。"""
    music_memory = MagicMock()
    music_memory.add_recommendation_feedback.side_effect = [
        Exception("DB locked"),
        None,
    ]
    writer = TieredFeedbackWriter(music_memory=music_memory)

    writer.write([
        (_rec(selected="rec1"), _result("positive")),
        (_rec(selected="rec2"), _result("positive")),
    ])

    assert music_memory.add_recommendation_feedback.call_count == 2

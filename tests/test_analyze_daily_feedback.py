"""Smoke tests for scripts/analyze_daily_feedback.py CLI entry.

Test scope: wiring is correct, output files generated. Underlying logic
already covered by test_feedback_batch / test_feedback_analyzer /
test_tiered_feedback_writer.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_agents.feedback_analyzer import FeedbackResult, MusicFeedbackAnalyzer
from intent_agents.recommendation import Recommendation, append_recommendation


def _ts_for_date(date_str: str, hour: int = 14) -> float:
    return datetime.strptime(date_str, "%Y-%m-%d").replace(hour=hour).timestamp()


@pytest.mark.asyncio
async def test_run_with_empty_jsonl_produces_clean_reports(tmp_path):
    from scripts.analyze_daily_feedback import run

    music_memory = MagicMock()
    transcript_store = MagicMock()
    transcript_store.get_recent = MagicMock(return_value=[])

    summary = await run(
        "2026-05-19",
        recs_path=tmp_path / "empty.jsonl",
        output_dir=tmp_path,
        music_memory=music_memory,
        transcript_store=transcript_store,
        analyzers={"music": MusicFeedbackAnalyzer(router=MagicMock())},
    )

    assert summary["total"] == 0
    assert summary["audit_lines"] == 0
    analysis = (tmp_path / "feedback_analysis_2026-05-19.md").read_text(encoding="utf-8")
    audit = (tmp_path / "audit_2026-05-19.md").read_text(encoding="utf-8")
    assert "No recommendations" in analysis
    assert "No anomalies" in audit
    # Empty case must NOT have written to music_memory
    music_memory.add_recommendation_feedback.assert_not_called()


@pytest.mark.asyncio
async def test_run_processes_recs_and_writes_t1(tmp_path):
    from scripts.analyze_daily_feedback import run

    log = tmp_path / "rec.jsonl"
    ts = _ts_for_date("2026-05-19")
    append_recommendation(
        Recommendation(
            ts=ts, agent="music", speaker="大肚", trigger="queue_empty",
            selected="周杰倫 夜曲", reason_internal="r", explanation_uttered="e",
            feedback_window_s=300, channel_state={},
        ),
        path=log,
    )

    # Mock analyzer returns high-confidence positive
    mock_analyzer = MagicMock()
    mock_analyzer.agent_type = "music"
    mock_analyzer.analyze = AsyncMock(return_value=FeedbackResult(
        sentiment="positive", confidence=0.85, reason="user said 讚",
    ))

    music_memory = MagicMock()
    transcript_store = MagicMock()
    transcript_store.get_recent = MagicMock(return_value=[])

    summary = await run(
        "2026-05-19",
        recs_path=log,
        output_dir=tmp_path,
        music_memory=music_memory,
        transcript_store=transcript_store,
        analyzers={"music": mock_analyzer},
    )

    assert summary["total"] == 1
    music_memory.add_recommendation_feedback.assert_called_once_with(
        "大肚", "周杰倫 夜曲", "liked",
    )
    analysis = (tmp_path / "feedback_analysis_2026-05-19.md").read_text(encoding="utf-8")
    assert "周杰倫 夜曲" in analysis
    assert "positive" in analysis


@pytest.mark.asyncio
async def test_dry_run_skips_t1_writes(tmp_path):
    from scripts.analyze_daily_feedback import run

    log = tmp_path / "rec.jsonl"
    ts = _ts_for_date("2026-05-19")
    append_recommendation(
        Recommendation(
            ts=ts, agent="music", speaker="大肚", trigger="t",
            selected="x", reason_internal="r", explanation_uttered="e",
            feedback_window_s=300, channel_state={},
        ),
        path=log,
    )

    mock_analyzer = MagicMock()
    mock_analyzer.agent_type = "music"
    mock_analyzer.analyze = AsyncMock(return_value=FeedbackResult(
        sentiment="positive", confidence=0.9, reason="ok",
    ))

    music_memory = MagicMock()
    transcript_store = MagicMock()
    transcript_store.get_recent = MagicMock(return_value=[])

    summary = await run(
        "2026-05-19",
        recs_path=log,
        output_dir=tmp_path,
        music_memory=music_memory,
        transcript_store=transcript_store,
        analyzers={"music": mock_analyzer},
        dry_run=True,
    )

    assert summary["dry_run"] is True
    # Reports still written
    assert (tmp_path / "feedback_analysis_2026-05-19.md").exists()
    # But T1 store write skipped
    music_memory.add_recommendation_feedback.assert_not_called()


@pytest.mark.asyncio
async def test_low_confidence_emits_audit_line(tmp_path):
    from scripts.analyze_daily_feedback import run

    log = tmp_path / "rec.jsonl"
    ts = _ts_for_date("2026-05-19")
    append_recommendation(
        Recommendation(
            ts=ts, agent="music", speaker="大肚", trigger="t",
            selected="周杰倫 夜曲", reason_internal="r", explanation_uttered="e",
            feedback_window_s=300, channel_state={},
        ),
        path=log,
    )

    mock_analyzer = MagicMock()
    mock_analyzer.agent_type = "music"
    mock_analyzer.analyze = AsyncMock(return_value=FeedbackResult(
        sentiment="neutral", confidence=0.0, reason="llm_error: timeout",
    ))

    summary = await run(
        "2026-05-19",
        recs_path=log,
        output_dir=tmp_path,
        music_memory=MagicMock(),
        transcript_store=MagicMock(get_recent=MagicMock(return_value=[])),
        analyzers={"music": mock_analyzer},
    )

    assert summary["audit_lines"] >= 1
    audit_text = (tmp_path / "audit_2026-05-19.md").read_text(encoding="utf-8")
    assert "llm_error" in audit_text or "low_confidence" in audit_text


def test_main_help_runs(capsys):
    """Smoke test: CLI parses --help without error."""
    from scripts.analyze_daily_feedback import main
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0

"""TDD：NightlyFeedbackBatch orchestrator.

讀 records/agent_recommendations.jsonl → 對每筆 rec 抓 speaker 在
[rec.ts, rec.ts + feedback_window_s] 的 utt 窗口 → 派給對應 analyzer →
回傳 (rec, FeedbackResult) tuples。

Orchestrator 不寫回 store（那是下一層 tiered writer 的事）。
Orchestrator 也不是 agent，是純驅動器 — 命名刻意 Batch 不 Agent。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_agents.feedback_analyzer import FeedbackResult, Utterance
from intent_agents.feedback_batch import NightlyFeedbackBatch
from intent_agents.recommendation import Recommendation, append_recommendation


# ── Fixtures ───────────────────────────────────────────────────────────────

def _ts_for_local_date(date_str: str, hour: int = 14) -> float:
    """Unix ts for given local-date date_str (YYYY-MM-DD) at given hour."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=hour)
    return dt.timestamp()


def _rec(agent: str, speaker: str, ts: float,
         selected: str = "x", window_s: int = 300) -> Recommendation:
    return Recommendation(
        ts=ts, agent=agent, speaker=speaker, trigger="t",
        selected=selected, reason_internal="r",
        explanation_uttered="e", feedback_window_s=window_s,
        channel_state={},
    )


def _fake_analyzer(agent_type: str, sentiment: str = "positive",
                   confidence: float = 0.8):
    """A MagicMock analyzer that mimics FeedbackAnalyzer protocol."""
    a = MagicMock()
    a.agent_type = agent_type
    a.analyze = AsyncMock(return_value=FeedbackResult(
        sentiment=sentiment, confidence=confidence, reason="mock",
    ))
    return a


def _fetcher_returning(utts_per_speaker: dict[str, list[Utterance]]):
    """Build a transcript_fetcher(speaker, start_ts, end_ts) → list[Utterance].

    Returns utts from utts_per_speaker[speaker] filtered to the [start, end] window.
    """
    def _fetch(speaker: str, start_ts: float, end_ts: float) -> list[Utterance]:
        return [u for u in utts_per_speaker.get(speaker, [])
                if start_ts <= u.timestamp <= end_ts]
    return _fetch


# ── 1. Empty / no-op cases ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_jsonl_returns_empty(tmp_path):
    log = tmp_path / "rec.jsonl"
    batch = NightlyFeedbackBatch(
        analyzers={"music": _fake_analyzer("music")},
        transcript_fetcher=_fetcher_returning({}),
        recommendations_path=log,
    )
    out = await batch.run_for_date("2026-05-19")
    assert out == []


@pytest.mark.asyncio
async def test_missing_jsonl_file_returns_empty(tmp_path):
    batch = NightlyFeedbackBatch(
        analyzers={"music": _fake_analyzer("music")},
        transcript_fetcher=_fetcher_returning({}),
        recommendations_path=tmp_path / "no_such.jsonl",
    )
    out = await batch.run_for_date("2026-05-19")
    assert out == []


# ── 2. Date filtering ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_filters_by_local_date(tmp_path):
    """只處理指定日期內的 rec，其他日期 skip。"""
    log = tmp_path / "rec.jsonl"
    target_ts = _ts_for_local_date("2026-05-19", hour=10)
    other_ts = _ts_for_local_date("2026-05-20", hour=10)

    append_recommendation(_rec("music", "大肚", target_ts, selected="target_day"), path=log)
    append_recommendation(_rec("music", "大肚", other_ts, selected="other_day"), path=log)

    batch = NightlyFeedbackBatch(
        analyzers={"music": _fake_analyzer("music")},
        transcript_fetcher=_fetcher_returning({}),
        recommendations_path=log,
    )
    out = await batch.run_for_date("2026-05-19")
    assert len(out) == 1
    rec, _ = out[0]
    assert rec.selected == "target_day"


# ── 3. Dispatch by agent type ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatches_to_matching_analyzer(tmp_path):
    log = tmp_path / "rec.jsonl"
    ts = _ts_for_local_date("2026-05-19")
    append_recommendation(_rec("music", "大肚", ts), path=log)
    append_recommendation(_rec("topic", "露", ts + 60), path=log)

    music_a = _fake_analyzer("music", sentiment="positive")
    topic_a = _fake_analyzer("topic", sentiment="negative")

    batch = NightlyFeedbackBatch(
        analyzers={"music": music_a, "topic": topic_a},
        transcript_fetcher=_fetcher_returning({}),
        recommendations_path=log,
    )
    out = await batch.run_for_date("2026-05-19")
    assert len(out) == 2
    # Each analyzer called exactly once
    music_a.analyze.assert_awaited_once()
    topic_a.analyze.assert_awaited_once()
    # Music rec gets music's sentiment, topic gets topic's
    sentiments = {r.agent: res.sentiment for r, res in out}
    assert sentiments["music"] == "positive"
    assert sentiments["topic"] == "negative"


@pytest.mark.asyncio
async def test_unknown_agent_type_skipped_silently(tmp_path):
    """rec.agent 沒有對應 analyzer → skip 不炸（未來新 agent 還沒加 analyzer 時 graceful）。"""
    log = tmp_path / "rec.jsonl"
    ts = _ts_for_local_date("2026-05-19")
    append_recommendation(_rec("music", "大肚", ts), path=log)
    append_recommendation(_rec("future_agent", "大肚", ts + 60), path=log)

    music_a = _fake_analyzer("music")
    batch = NightlyFeedbackBatch(
        analyzers={"music": music_a},
        transcript_fetcher=_fetcher_returning({}),
        recommendations_path=log,
    )
    out = await batch.run_for_date("2026-05-19")
    assert len(out) == 1
    assert out[0][0].agent == "music"


# ── 4. Transcript window fetching ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_transcript_fetcher_called_with_correct_window(tmp_path):
    log = tmp_path / "rec.jsonl"
    ts = _ts_for_local_date("2026-05-19")
    append_recommendation(
        _rec("music", "大肚", ts, window_s=300),
        path=log,
    )

    fetcher = MagicMock(return_value=[])
    batch = NightlyFeedbackBatch(
        analyzers={"music": _fake_analyzer("music")},
        transcript_fetcher=fetcher,
        recommendations_path=log,
    )
    await batch.run_for_date("2026-05-19")

    fetcher.assert_called_once_with("大肚", ts, ts + 300)


@pytest.mark.asyncio
async def test_utts_passed_to_analyzer(tmp_path):
    log = tmp_path / "rec.jsonl"
    ts = _ts_for_local_date("2026-05-19")
    append_recommendation(_rec("music", "大肚", ts), path=log)

    utts = [Utterance("大肚", "好聽", ts + 30)]
    analyzer = _fake_analyzer("music")
    batch = NightlyFeedbackBatch(
        analyzers={"music": analyzer},
        transcript_fetcher=_fetcher_returning({"大肚": utts}),
        recommendations_path=log,
    )
    await batch.run_for_date("2026-05-19")

    call_args = analyzer.analyze.call_args
    passed_utts = call_args.kwargs.get("utts_in_window") or call_args.args[1]
    assert passed_utts == utts


# ── 5. Failure isolation ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_analyzer_exception_isolated_doesnt_break_batch(tmp_path):
    """單一 analyzer 炸 → 跳過該 rec，其他繼續。"""
    log = tmp_path / "rec.jsonl"
    ts = _ts_for_local_date("2026-05-19")
    append_recommendation(_rec("music", "大肚", ts, selected="rec1"), path=log)
    append_recommendation(_rec("music", "大肚", ts + 60, selected="rec2"), path=log)

    analyzer = MagicMock()
    analyzer.agent_type = "music"
    analyzer.analyze = AsyncMock(side_effect=[
        Exception("analyzer crash"),
        FeedbackResult(sentiment="positive", confidence=0.8, reason="ok"),
    ])

    batch = NightlyFeedbackBatch(
        analyzers={"music": analyzer},
        transcript_fetcher=_fetcher_returning({}),
        recommendations_path=log,
    )
    out = await batch.run_for_date("2026-05-19")
    assert len(out) == 1
    assert out[0][0].selected == "rec2"


# ── 6. Order preservation ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_results_in_jsonl_order(tmp_path):
    log = tmp_path / "rec.jsonl"
    ts = _ts_for_local_date("2026-05-19")
    for i in range(3):
        append_recommendation(
            _rec("music", "大肚", ts + i, selected=f"song{i}"),
            path=log,
        )

    batch = NightlyFeedbackBatch(
        analyzers={"music": _fake_analyzer("music")},
        transcript_fetcher=_fetcher_returning({}),
        recommendations_path=log,
    )
    out = await batch.run_for_date("2026-05-19")
    selected_order = [r.selected for r, _ in out]
    assert selected_order == ["song0", "song1", "song2"]

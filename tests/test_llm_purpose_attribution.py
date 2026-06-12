"""LLM purpose 歸因修正 — asyncio.wait_for 包住 _call_llm 的誤歸因。

2026-06-12：llm_purpose_breakdown 最大戶 "wait_for"（43 筆/天）是歸因假象：
_call_llm 的 frame 自動歸因抓到 asyncio.wait_for 而非真 caller。真身是
mood_sensor / recall_handler / session_summarizer 三個呼叫點。修法＝顯式傳
purpose=。其中 _classify_mood / summarize_window 在 BACKGROUND_PURPOSES 內，
歸因修對後自動吃到背景降權（高峰把 Groq 留給 reactive）。
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_mood_sensor_passes_classify_mood_purpose():
    from mood_sensor import MoodSensor

    router = MagicMock()
    router._call_llm = AsyncMock(return_value="放鬆")
    sensor = MoodSensor(
        transcript_store=MagicMock(),
        groq_client=MagicMock(),
        temperature_monitor=MagicMock(),
        router=router,
    )

    await sensor._classify_mood([
        {"speaker": "狗與露", "text": "今天好累", "timestamp": time.time()},
        {"speaker": "showay", "text": "喝杯茶吧", "timestamp": time.time()},
    ])

    router._call_llm.assert_awaited_once()
    assert router._call_llm.call_args.kwargs.get("purpose") == "_classify_mood"


@pytest.mark.asyncio
async def test_session_summarizer_passes_summarize_window_purpose():
    from summary_store import SummaryStore
    from session_summarizer import SessionSummarizer

    transcript_store = MagicMock()
    transcript_store.get_recent.return_value = [
        {"speaker": "狗與露", "text": "下次帶 showay 去吃拉麵", "timestamp": 100.0},
        {"speaker": "showay", "text": "好啊", "timestamp": 110.0},
        {"speaker": "狗與露", "text": "順便查 API 文件", "timestamp": 120.0},
    ]
    router = MagicMock()
    router._call_llm = AsyncMock(return_value='{"summary": "聊拉麵", "tasks": []}')

    summ = SessionSummarizer(
        transcript_store=transcript_store,
        summary_store=SummaryStore(db_path=":memory:"),
        groq_client=MagicMock(),
        owner_speaker="狗與露",
        router=router,
    )
    await summ.summarize_window(guild_id=1, window_start=0.0, window_end=300.0)

    router._call_llm.assert_awaited_once()
    assert router._call_llm.call_args.kwargs.get("purpose") == "summarize_window"


@pytest.mark.asyncio
async def test_recall_handler_passes_recall_5w2h_purpose():
    from recall_handler import RecallHandler

    _summaries = [{"window_end": time.time() - 60, "summary_text": "剛剛在聊拉麵"}]
    summary_store = MagicMock()
    summary_store.get_summaries.return_value = _summaries
    summary_store.search.return_value = _summaries
    transcript_store = MagicMock()
    transcript_store.get_recent.return_value = []
    router = MagicMock()
    router._call_llm = AsyncMock(return_value="你們剛聊了拉麵")

    handler = RecallHandler(
        summary_store=summary_store,
        task_store=MagicMock(),
        transcript_store=transcript_store,
        groq_client=MagicMock(),
        guild_id=1,
        owner_speaker="狗與露",
        router=router,
    )
    await handler.handle("狗與露", "我們剛剛聊了什麼")

    router._call_llm.assert_awaited_once()
    assert router._call_llm.call_args.kwargs.get("purpose") == "recall_5w2h"


def test_recall_5w2h_is_known_purpose():
    """新 purpose 必須進 KNOWN_PURPOSES，否則每次 dispatch 都噴 typo warning。"""
    from llm_agents.base import KNOWN_PURPOSES

    assert "recall_5w2h" in KNOWN_PURPOSES

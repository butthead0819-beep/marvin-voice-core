"""
TDD tests for SummaryStore and SessionSummarizer.

Run with:
    pytest tests/test_session_summarizer.py -v
"""
from __future__ import annotations

import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock


# ═══════════════════════════════════════════════════════
# SummaryStore tests
# ═══════════════════════════════════════════════════════

def _make_summary_store(db_path=":memory:"):
    from summary_store import SummaryStore
    return SummaryStore(db_path=db_path)


def test_summary_store_save_and_get():
    store = _make_summary_store()
    now = time.time()
    store.save_summary(
        guild_id=1,
        window_start=now - 300,
        window_end=now,
        summary_text="Jack 討論了週末計劃，提到要去爬山。",
        speakers=["狗與鹿", "showay"],
    )
    results = store.get_summaries(guild_id=1, hours=1)
    assert len(results) == 1
    assert results[0]["summary_text"] == "Jack 討論了週末計劃，提到要去爬山。"
    assert "狗與鹿" in results[0]["speakers"]


def test_summary_store_excludes_old_summaries():
    store = _make_summary_store()
    old = time.time() - 25 * 3600
    store.save_summary(guild_id=1, window_start=old - 300, window_end=old,
                       summary_text="很舊的對話", speakers=["狗與鹿"])
    assert store.get_summaries(guild_id=1, hours=24) == []


def test_summary_store_keyword_search():
    store = _make_summary_store()
    now = time.time()
    store.save_summary(guild_id=1, window_start=now - 300, window_end=now,
                       summary_text="討論了 API 整合方案", speakers=["狗與鹿"])
    store.save_summary(guild_id=1, window_start=now - 600, window_end=now - 300,
                       summary_text="聊了週末要去哪裡玩", speakers=["狗與鹿"])
    results = store.search(guild_id=1, keyword="API")
    assert len(results) == 1
    assert "API" in results[0]["summary_text"]


def test_summary_store_get_by_window():
    store = _make_summary_store()
    store.save_summary(guild_id=1, window_start=1000.0, window_end=1300.0,
                       summary_text="窗口A", speakers=["狗與鹿"])
    store.save_summary(guild_id=1, window_start=2000.0, window_end=2300.0,
                       summary_text="窗口B", speakers=["狗與鹿"])
    results = store.get_by_window(guild_id=1, window_start=900.0, window_end=1400.0)
    assert len(results) == 1
    assert results[0]["summary_text"] == "窗口A"


# ═══════════════════════════════════════════════════════
# SessionSummarizer helpers
# ═══════════════════════════════════════════════════════

def _make_transcript_store(utterances: list[dict]):
    ts = MagicMock()
    ts.get_recent.return_value = utterances
    return ts


def _make_groq_response(content: str):
    resp = MagicMock()
    resp.choices[0].message.content = content
    return resp


def _make_summarizer(utterances, groq_response_content=None, on_commitment_detected=None):
    from summary_store import SummaryStore
    from session_summarizer import SessionSummarizer

    summary_store = SummaryStore(db_path=":memory:")
    transcript_store = _make_transcript_store(utterances)
    groq_client = MagicMock()

    if groq_response_content is not None:
        groq_client.chat.completions.create = AsyncMock(
            return_value=_make_groq_response(groq_response_content)
        )

    return SessionSummarizer(
        transcript_store=transcript_store,
        summary_store=summary_store,
        groq_client=groq_client,
        owner_speaker="狗與鹿",
        on_commitment_detected=on_commitment_detected,
    ), summary_store


_VALID_LLM_RESPONSE = json.dumps({
    "summary": "Jack 答應了下次帶 showay 去吃拉麵，並提到要查 API 文件。",
    "commitments": [
        {
            "speaker": "狗與鹿",
            "text": "下次帶 showay 去吃拉麵",
            "type": "promise",
            "target": "showay",
            "due_date": None,
        },
        {
            "speaker": "狗與鹿",
            "text": "查 API 文件",
            "type": "todo",
            "target": None,
            "due_date": None,
        },
    ],
}, ensure_ascii=False)


def _make_sparse_utterances():
    now = time.time()
    return [
        {"speaker": "狗與鹿", "text": "嗨", "timestamp": now - 200},
        {"speaker": "狗與鹿", "text": "好", "timestamp": now - 190},
    ]


def _make_multi_speaker_utterances():
    now = time.time()
    return [
        {"speaker": "狗與鹿", "text": "我下次帶你去吃拉麵", "timestamp": now - 200},
        {"speaker": "showay",  "text": "好啊，那 API 文件呢？", "timestamp": now - 190},
        {"speaker": "狗與鹿", "text": "我等等去查", "timestamp": now - 180},
    ]


def _make_single_speaker_utterances():
    now = time.time()
    return [
        {"speaker": "狗與鹿", "text": "我等等要去買菜", "timestamp": now - 200},
        {"speaker": "狗與鹿", "text": "還有要查一下 API 文件", "timestamp": now - 190},
        {"speaker": "狗與鹿", "text": "然後記得寄信", "timestamp": now - 180},
    ]


# ═══════════════════════════════════════════════════════
# SessionSummarizer tests
# ═══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_summarizer_skips_sparse_window():
    now = time.time()
    summarizer, summary_store = _make_summarizer(_make_sparse_utterances())
    await summarizer.summarize_window(guild_id=1, window_start=now - 300, window_end=now)
    assert summary_store.get_summaries(guild_id=1) == []
    summarizer.groq_client.chat.completions.create.assert_not_called()


@pytest.mark.asyncio
async def test_summarizer_saves_summary_from_llm():
    utterances = _make_multi_speaker_utterances()
    now = time.time()
    summarizer, summary_store = _make_summarizer(utterances, _VALID_LLM_RESPONSE)
    await summarizer.summarize_window(guild_id=1, window_start=now - 300, window_end=now)
    results = summary_store.get_summaries(guild_id=1)
    assert len(results) == 1
    assert "拉麵" in results[0]["summary_text"]


@pytest.mark.asyncio
async def test_summarizer_multi_speaker_calls_callback():
    """多人討論的承諾 → 呼叫 callback，不直接存 task。"""
    captured = []
    utterances = _make_multi_speaker_utterances()
    now = time.time()
    summarizer, _ = _make_summarizer(
        utterances, _VALID_LLM_RESPONSE,
        on_commitment_detected=captured.append,
    )
    await summarizer.summarize_window(guild_id=1, window_start=now - 300, window_end=now)
    assert len(captured) == 2
    texts = [c.task_text for c in captured]
    assert any("拉麵" in t for t in texts)
    assert any("API 文件" in t for t in texts)


@pytest.mark.asyncio
async def test_summarizer_single_speaker_skips_callback():
    """單人窗口（自言自語）→ callback 不呼叫。"""
    captured = []
    utterances = _make_single_speaker_utterances()
    now = time.time()
    summarizer, _ = _make_summarizer(
        utterances, _VALID_LLM_RESPONSE,
        on_commitment_detected=captured.append,
    )
    await summarizer.summarize_window(guild_id=1, window_start=now - 300, window_end=now)
    assert captured == []


@pytest.mark.asyncio
async def test_summarizer_inbound_commitment_direction():
    """promise 類型 → direction=inbound。"""
    captured = []
    utterances = _make_multi_speaker_utterances()
    now = time.time()
    summarizer, _ = _make_summarizer(
        utterances, _VALID_LLM_RESPONSE,
        on_commitment_detected=captured.append,
    )
    await summarizer.summarize_window(guild_id=1, window_start=now - 300, window_end=now)
    assert all(c.direction == "inbound" for c in captured)


@pytest.mark.asyncio
async def test_summarizer_outbound_when_owner_assigns_others():
    llm_resp = json.dumps({
        "summary": "Jack 交辦 showay 查合約",
        "commitments": [{"speaker": "狗與鹿", "text": "showay 去確認合約條款",
                         "type": "todo", "target": "showay", "due_date": None}],
    }, ensure_ascii=False)
    captured = []
    utterances = _make_multi_speaker_utterances()
    now = time.time()
    summarizer, _ = _make_summarizer(
        utterances, llm_resp,
        on_commitment_detected=captured.append,
    )
    await summarizer.summarize_window(guild_id=1, window_start=now - 300, window_end=now)
    assert len(captured) == 1
    assert captured[0].direction == "outbound"
    assert captured[0].assignee == "showay"


@pytest.mark.asyncio
async def test_summarizer_handles_invalid_json_gracefully():
    utterances = _make_multi_speaker_utterances()
    now = time.time()
    captured = []
    summarizer, summary_store = _make_summarizer(
        utterances, "這不是 JSON，LLM 壞掉了",
        on_commitment_detected=captured.append,
    )
    await summarizer.summarize_window(guild_id=1, window_start=now - 300, window_end=now)
    results = summary_store.get_summaries(guild_id=1)
    assert len(results) == 1
    assert captured == []


@pytest.mark.asyncio
async def test_summarizer_handles_llm_timeout_gracefully():
    import asyncio as _asyncio
    utterances = _make_multi_speaker_utterances()
    now = time.time()
    summarizer, summary_store = _make_summarizer(utterances)
    summarizer.groq_client.chat.completions.create = AsyncMock(
        side_effect=_asyncio.TimeoutError()
    )
    await summarizer.summarize_window(guild_id=1, window_start=now - 300, window_end=now)
    assert summary_store.get_summaries(guild_id=1) == []


@pytest.mark.asyncio
async def test_summarizer_source_quote_in_pending_confirmation():
    """PendingConfirmation.source_quote 包含原始 STT 原話。"""
    captured = []
    utterances = _make_multi_speaker_utterances()
    now = time.time()
    summarizer, _ = _make_summarizer(
        utterances, _VALID_LLM_RESPONSE,
        on_commitment_detected=captured.append,
    )
    await summarizer.summarize_window(guild_id=1, window_start=now - 300, window_end=now)
    all_quotes = " ".join(c.source_quote for c in captured)
    assert "拉麵" in all_quotes or "API" in all_quotes or "查" in all_quotes


@pytest.mark.asyncio
async def test_summarizer_pending_confirmation_has_ttl():
    """PendingConfirmation.expires_at 應在未來。"""
    captured = []
    utterances = _make_multi_speaker_utterances()
    now = time.time()
    summarizer, _ = _make_summarizer(
        utterances, _VALID_LLM_RESPONSE,
        on_commitment_detected=captured.append,
    )
    await summarizer.summarize_window(guild_id=1, window_start=now - 300, window_end=now)
    assert all(c.expires_at > time.time() for c in captured)

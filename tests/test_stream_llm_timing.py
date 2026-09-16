"""TDD — stream_llm（使用者實際等待的主回應路徑）過去完全沒有 latency instrumentation。

llm_routing.jsonl 的 log_dispatch 只掛在 _call_llm 的 bus 路徑；stream_llm（Groq→
Cerebras→Gemini 逐層 fallback，Marvin 開口回答走的就是這條）沒有任何計時，導致
analyze_latency_breakdown.py 的「回應 LLM」段長年混進背景任務（社交分析/記憶萃取…）
latency，完全沒測到使用者真正等待的時間。

修法：每層 tier 第一個 chunk 抵達時（或該層失敗時）寫一筆 route="stream" 的
log_dispatch，latency_ms = 首個 chunk 的等待時間（跟 TTS_TIMING 的 first_audio
語意一致）。每層只記一筆，不隨 chunk 數重複寫。
"""
from __future__ import annotations

import json

import pytest
from unittest.mock import AsyncMock, MagicMock


def _make_router():
    from gemini_router_llm import GeminiRouterLLMMixin
    obj = GeminiRouterLLMMixin.__new__(GeminiRouterLLMMixin)
    obj.dna = {}
    obj.groq_dedicated_client = MagicMock()
    obj.groq_fallback_model = "groq-m"
    obj.cerebras_client = None
    obj.cerebras_model = None
    obj.is_exhausted = True
    obj.budget = MagicMock(is_circuit_open=MagicMock(return_value=True))
    obj.model_name = "gemini-test"
    obj._reset_tier_to_primary = AsyncMock()
    return obj


def _read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


@pytest.mark.asyncio
async def test_stream_llm_groq_success_logs_one_stream_entry(monkeypatch, tmp_path):
    from llm_agents import metrics
    log_path = tmp_path / "llm_routing.jsonl"
    monkeypatch.setattr(metrics, "_LOG_PATH", log_path)

    router = _make_router()

    async def _fake_stream_groq(*a, **kw):
        yield "hello"
        yield " world"

    router._stream_groq = _fake_stream_groq

    chunks = [c async for c in router.stream_llm("sys", "user")]

    assert chunks == ["hello", " world"]
    entries = _read_jsonl(log_path)
    assert len(entries) == 1, f"每層 tier 只該記一筆（不隨 chunk 數重複），實際 {len(entries)} 筆"
    e = entries[0]
    assert e["route"] == "stream"
    assert e["purpose"] == "stream_llm"
    assert e["provider"] == "groq"
    assert e["model"] == "groq-m"
    assert e["success"] is True
    assert e["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_stream_llm_groq_failure_before_any_chunk_logs_failure_and_falls_through(monkeypatch, tmp_path):
    from llm_agents import metrics
    log_path = tmp_path / "llm_routing.jsonl"
    monkeypatch.setattr(metrics, "_LOG_PATH", log_path)

    router = _make_router()

    async def _broken_stream_groq(*a, **kw):
        raise RuntimeError("groq boom")
        yield  # noqa: unreachable — makes this an async generator function

    router._stream_groq = _broken_stream_groq
    # cerebras/cloud 都不可用（None/exhausted）→ 全滅，驗證失敗有被記到
    chunks = [c async for c in router.stream_llm("sys", "user")]

    assert chunks == []
    entries = _read_jsonl(log_path)
    assert len(entries) == 1
    e = entries[0]
    assert e["route"] == "stream"
    assert e["provider"] == "groq"
    assert e["success"] is False
    assert "groq boom" in e["error"]

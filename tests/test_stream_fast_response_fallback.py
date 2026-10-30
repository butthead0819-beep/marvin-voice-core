"""主聊天 stream_fast_response 雲端 503 備援（2026-10-21 事故）。

10/21 兩次 edge case：Gemini _stream_cloud 503 一個字都沒出，直接落到外層 except
念出「宇宙的熵值突然激增」錯誤台詞。修法：雲端一個字都沒出就失敗 → 改走 stream_llm
（Groq 優先）備援；已經念出一半才失敗，不能半路換模型重講，維持原有錯誤台詞行為。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import gemini_router_llm as grl
import pipeline_timing


def _make_fake_router(stream_cloud, stream_llm):
    """最小 fake：只給 stream_fast_response 實際用到的屬性/方法，其餘用 mixin 真方法。"""
    fake = object.__new__(grl.GeminiRouterLLMMixin)
    fake.dna = {}
    fake._session_call_count = 0
    fake.prompt_manager = MagicMock()
    fake.prompt_manager.get_instruction.return_value = "system prompt"
    fake.memory = MagicMock()
    fake.current_game = None
    fake._intent_search_cache = {}
    fake.provider = "gemini"
    fake.is_exhausted = False
    fake.budget = MagicMock()
    fake.budget.is_circuit_open.return_value = False
    fake.cleaner_model = "flash-lite"
    fake.short_term_dialogue = []
    fake.last_stream_error_time = 0.0
    fake._maybe_advance_relationship = MagicMock()
    fake._stream_cloud = stream_cloud
    fake.stream_llm = stream_llm
    return fake


async def _collect(fake, query="測試訊息"):
    out = []
    async for chunk in fake.stream_fast_response("A", query):
        out.append(chunk)
    return "".join(out)


@pytest.mark.asyncio
async def test_cloud_fails_before_any_chunk_falls_back_to_stream_llm():
    async def _raising_cloud(*a, **kw):
        raise RuntimeError("503")
        yield  # pragma: no cover

    async def _fallback_llm(*a, **kw):
        yield "備援"
        yield "回應"

    fake = _make_fake_router(_raising_cloud, _fallback_llm)
    text = await _collect(fake)

    assert text == "備援回應"
    assert "宇宙的熵值" not in text


@pytest.mark.asyncio
async def test_cloud_fails_after_partial_chunk_does_not_fall_back():
    calls = {"stream_llm": 0}

    async def _partial_cloud(*a, **kw):
        yield "前半"
        raise RuntimeError("fail after partial")

    async def _fallback_llm(*a, **kw):
        calls["stream_llm"] += 1
        yield "不該被呼叫"

    fake = _make_fake_router(_partial_cloud, _fallback_llm)
    text = await _collect(fake)

    assert calls["stream_llm"] == 0
    assert "前半" in text


@pytest.mark.asyncio
async def test_both_cloud_and_fallback_fail_keeps_original_error_line():
    async def _raising_cloud(*a, **kw):
        raise RuntimeError("503")
        yield  # pragma: no cover

    async def _raising_llm(*a, **kw):
        raise RuntimeError("groq down too")
        yield  # pragma: no cover

    fake = _make_fake_router(_raising_cloud, _raising_llm)
    text = await _collect(fake)

    assert "宇宙的熵值" in text


# ── E1：主 LLM 路徑打點（llm_route / first_chunk） ────────────────────────────

@pytest.mark.asyncio
async def test_cloud_success_marks_cloud_route_and_first_chunk():
    async def _cloud(*a, **kw):
        yield "你好"

    async def _unused_llm(*a, **kw):
        yield "不該被呼叫"  # pragma: no cover

    fake = _make_fake_router(_cloud, _unused_llm)
    pipeline_timing.start()
    await _collect(fake)

    snap = pipeline_timing.snapshot()
    assert snap["llm_route"] == "cloud"
    assert "llm_start" in snap
    assert "first_chunk" in snap


@pytest.mark.asyncio
async def test_cloud_fails_before_any_chunk_marks_fallback_route():
    async def _raising_cloud(*a, **kw):
        raise RuntimeError("503")
        yield  # pragma: no cover

    async def _fallback_llm(*a, **kw):
        yield "備援"

    fake = _make_fake_router(_raising_cloud, _fallback_llm)
    pipeline_timing.start()
    await _collect(fake)

    snap = pipeline_timing.snapshot()
    assert snap["llm_route"] == "cloud→fallback"
    assert "first_chunk" in snap


@pytest.mark.asyncio
async def test_speculative_response_does_not_leak_llm_start_into_caller_dict():
    """_speculative_response 是獨立 prefetch task，呼叫前 detach() 讓它不寫進
    呼叫端（主路徑）的 timing dict，否則預取會把 llm_start/first_chunk 誤標進主路徑量測。"""
    import asyncio

    async def _cloud(*a, **kw):
        yield "預取內容"

    async def _unused_llm(*a, **kw):
        yield "不該被呼叫"  # pragma: no cover

    fake = _make_fake_router(_cloud, _unused_llm)
    fake._prefetch_semaphore = asyncio.Semaphore(1)

    caller_dict = pipeline_timing.start()
    result = await fake._speculative_response("A", "測試訊息")

    assert result == "預取內容"
    assert "llm_start" not in caller_dict
    assert "first_chunk" not in caller_dict


@pytest.mark.asyncio
async def test_stream_cloud_first_chunk_ttft_timeout_falls_back_to_stream_llm(monkeypatch):
    """雲端 stream 物件立即取得，但第一個 chunk 超過 TTFT timeout → 自動 fallback 到 stream_llm。"""
    import asyncio
    from unittest.mock import AsyncMock

    monkeypatch.setenv("MARVIN_CLOUD_TTFT_TIMEOUT_S", "0.05")

    async def _slow_generator():
        await asyncio.sleep(0.2)
        yield MagicMock(text="慢速 chunk")

    fake = _make_fake_router(None, None)
    # 使用 mixin 原生的 _stream_cloud
    fake._stream_cloud = lambda *a, **kw: grl.GeminiRouterLLMMixin._stream_cloud(fake, *a, **kw)
    fake._cloud_rpm_window = []
    fake._CLOUD_RPM_LIMIT = 20
    fake.google_client = MagicMock()
    fake.google_client.aio.models.generate_content_stream = AsyncMock(return_value=_slow_generator())
    fake.model_name = "gemini-2.5-flash"
    fake._supports_thinking = lambda: False
    fake._try_acquire_chat_rpm_slot = lambda: True

    async def _fallback_llm(*a, **kw):
        yield "備援成功"

    fake.stream_llm = _fallback_llm

    chunks = []
    async for chunk in fake.stream_fast_response("Alice", "哈囉"):
        chunks.append(chunk)

    assert "".join(chunks) == "備援成功"


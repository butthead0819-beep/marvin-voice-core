"""TDD — Busted99 voice_parse.extract_guess_via_llm

驗項（mock LLM，驗 helper 行為）：
A) 中文數字「我猜五十七」→ 57
B) 模糊夾雜「應該是 38 吧」→ 38
C) 英文數字「ninety nine」→ 99
D) 多重數字（玩家更正）「七十、不對七十二」→ 72
E) 無數字「嗯不知道」→ None
F) 超範圍「兩百」→ None
G) LLM 例外 → None（graceful fallback）
H) LLM 回傳非 JSON → None
I) 空字串 → None（不浪費 LLM token）
"""

from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from game.busted99.voice_parse import extract_guess_via_llm


def _mock_client(payload: str | None, raise_exc: Exception | None = None):
    client = MagicMock()
    if raise_exc is not None:
        client.chat.completions.create = AsyncMock(side_effect=raise_exc)
    else:
        resp = MagicMock()
        resp.choices = [MagicMock(message=MagicMock(content=payload))]
        client.chat.completions.create = AsyncMock(return_value=resp)
    return client


def _payload(number):
    return json.dumps({"number": number}, ensure_ascii=False)


# ── A: 中文數字 ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chinese_digits_extracted():
    client = _mock_client(_payload(57))
    result = await extract_guess_via_llm("我猜五十七", 1, 99, llm_client=client)
    assert result == 57


# ── B: 模糊夾雜阿拉伯數字 ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_arabic_with_fillers():
    client = _mock_client(_payload(38))
    result = await extract_guess_via_llm("應該是 38 吧", 1, 99, llm_client=client)
    assert result == 38


# ── C: 英文數字 ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_english_number_words():
    client = _mock_client(_payload(99))
    result = await extract_guess_via_llm("我覺得是 ninety nine", 1, 99, llm_client=client)
    assert result == 99


# ── D: 玩家更正自己 ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_self_correction_takes_last():
    client = _mock_client(_payload(72))
    result = await extract_guess_via_llm("七十、不對七十二", 1, 99, llm_client=client)
    assert result == 72


# ── E: 無數字 ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_number_returns_none():
    client = _mock_client(_payload(None))
    result = await extract_guess_via_llm("嗯不知道", 1, 99, llm_client=client)
    assert result is None


# ── F: 超範圍 → None ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_out_of_range_rejected():
    # LLM 即使回 200，helper 也要拒絕（防 LLM 不守規則）
    client = _mock_client(_payload(200))
    result = await extract_guess_via_llm("兩百", 1, 99, llm_client=client)
    assert result is None


# ── G: LLM 例外 graceful fallback ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_exception_returns_none():
    client = _mock_client(None, raise_exc=ConnectionError("network down"))
    result = await extract_guess_via_llm("我猜五十", 1, 99, llm_client=client)
    assert result is None


# ── H: LLM 回非 JSON → None ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_garbage_returns_none():
    client = _mock_client("這不是 JSON")
    result = await extract_guess_via_llm("我猜五十", 1, 99, llm_client=client)
    assert result is None


# ── I: 空字串不打 LLM ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_text_skips_llm():
    client = _mock_client(_payload(50))
    result = await extract_guess_via_llm("", 1, 99, llm_client=client)
    assert result is None
    client.chat.completions.create.assert_not_called()


# ── J: 無 client（沒設 API key）→ None ───────────────────────────────────────

@pytest.mark.asyncio
async def test_no_client_returns_none(monkeypatch):
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_PAID_API_KEY", raising=False)
    result = await extract_guess_via_llm("我猜五十", 1, 99, llm_client=None)
    assert result is None


# ── K: 邊界值合法 ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_boundary_values_accepted():
    client = _mock_client(_payload(1))
    assert await extract_guess_via_llm("一", 1, 99, llm_client=client) == 1
    client = _mock_client(_payload(99))
    assert await extract_guess_via_llm("九十九", 1, 99, llm_client=client) == 99

"""Tests for game/clue_generator.py — router.complete() contract and prompt shape."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from game.clue_generator import generate_clue, judge_answer


# ---------------------------------------------------------------------------
# generate_clue — router protocol
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_clue_calls_complete():
    """generate_clue must call router.complete(system=..., user=...) exactly once."""
    router = MagicMock()
    router.complete = AsyncMock(return_value="神秘線索")
    clue = await generate_clue("蘋果汁", 1, [], router)
    router.complete.assert_called_once()
    assert clue == "神秘線索"


@pytest.mark.asyncio
async def test_generate_clue_passes_keyword_args():
    """complete() must be called with system= and user= keyword args."""
    router = MagicMock()
    router.complete = AsyncMock(return_value="ok")
    await generate_clue("蘋果汁", 1, [], router)
    _, kwargs = router.complete.call_args
    assert "system" in kwargs
    assert "user" in kwargs


@pytest.mark.asyncio
async def test_generate_clue_fallback_on_exception():
    """When router.complete() raises, returns the fallback string (does not re-raise)."""
    router = MagicMock()
    router.complete = AsyncMock(side_effect=Exception("API error"))
    clue = await generate_clue("蘋果汁", 1, [], router)
    assert "失敗" in clue


@pytest.mark.asyncio
async def test_generate_clue_fallback_when_complete_missing():
    """AttributeError from missing complete() is caught — returns fallback, never raises."""
    router = object()  # no complete attribute
    clue = await generate_clue("蘋果汁", 1, [], router)
    assert isinstance(clue, str)
    assert "失敗" in clue


# ---------------------------------------------------------------------------
# generate_clue — prompt content
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_clue_answer_not_in_system_prompt_after_removal():
    """System prompt must embed the answer (setter's job is to hint, not be told to avoid it
    after the fact; the rule in the prompt says '不可直接說出答案').
    We verify the system prompt contains the answer so the LLM knows what NOT to say."""
    captured = {}

    async def capture(**kwargs):
        captured.update(kwargs)
        return "ok"

    router = MagicMock()
    router.complete = capture
    await generate_clue("西瓜", 1, [], router)
    assert "西瓜" in captured["system"]


@pytest.mark.asyncio
async def test_generate_clue_prior_clues_included_from_round2():
    """From round 2 onwards, prior clues must appear in the system prompt."""
    captured = {}

    async def capture(**kwargs):
        captured.update(kwargs)
        return "ok"

    router = MagicMock()
    router.complete = capture
    prior = ["感覺很甜"]
    await generate_clue("西瓜", 2, prior, router)
    assert "感覺很甜" in captured["system"]


@pytest.mark.asyncio
async def test_generate_clue_no_prior_section_for_round1():
    """Round 1 has no prior clues — the prior section must be empty."""
    captured = {}

    async def capture(**kwargs):
        captured.update(kwargs)
        return "ok"

    router = MagicMock()
    router.complete = capture
    await generate_clue("西瓜", 1, [], router)
    assert "已有線索" not in captured["system"]


@pytest.mark.asyncio
async def test_generate_clue_round_clamps_to_valid_range():
    """round_num outside 1-5 should not crash (clamped internally)."""
    router = MagicMock()
    router.complete = AsyncMock(return_value="ok")
    clue = await generate_clue("西瓜", 0, [], router)
    assert clue == "ok"
    clue = await generate_clue("西瓜", 99, [], router)
    assert clue == "ok"


# ---------------------------------------------------------------------------
# judge_answer
# ---------------------------------------------------------------------------

def test_judge_answer_exact():
    assert judge_answer("蘋果汁", "蘋果汁") is True


def test_judge_answer_case_insensitive():
    assert judge_answer("Apple", "apple") is True


def test_judge_answer_strips_whitespace():
    assert judge_answer("蘋果汁", " 蘋果汁 ") is True


def test_judge_answer_wrong():
    assert judge_answer("蘋果汁", "西瓜汁") is False

"""TDD — MarvinPlayer guess output normalization.

Backstory: prompt says "只說出答案詞，不要解釋" but the LLM still ships
prefixes like 「我猜是...」 or trailing punctuation. With code-judge
fallback, "我猜是巨石強森" != "巨石強森" → wrong answer, wasted buzz.

Tests:
  A) Bare answer → returned unchanged
  B) "我猜是X"   → strips prefix → "X"
  C) "答案是X"   → strips prefix → "X"
  D) "X。"       → strips trailing period
  E) "X."        → strips half-width period
  F) "X!"        → strips exclamation
  G) "「X」"     → strips brackets
  H) Multi-line "X\n解釋..." → keeps only first line
  I) Long sentence containing answer → keep as-is (give judge a chance)
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from game.marvin_player import MarvinPlayer, _normalize_guess


def _fake_groq(text: str):
    c = MagicMock()
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = text
    c.chat.completions.create = AsyncMock(return_value=resp)
    return c


# ── Pure helper tests — no LLM mock needed ────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("巨石強森",        "巨石強森"),
    ("我猜是巨石強森",  "巨石強森"),
    ("答案是巨石強森",  "巨石強森"),
    ("應該是巨石強森",  "巨石強森"),
    ("巨石強森。",      "巨石強森"),
    ("巨石強森.",       "巨石強森"),
    ("巨石強森!",       "巨石強森"),
    ("巨石強森！",      "巨石強森"),
    ("巨石強森?",       "巨石強森"),
    ("巨石強森？",      "巨石強森"),
    ("「巨石強森」",    "巨石強森"),
    ('"巨石強森"',      "巨石強森"),
    ("'巨石強森'",      "巨石強森"),
    ("巨石強森\n這是好萊塢明星", "巨石強森"),    # only first line
    ("  巨石強森  ",    "巨石強森"),
    ("",                ""),
])
def test_normalize_guess(raw, expected):
    assert _normalize_guess(raw) == expected


# ── End-to-end: generate_guess wraps the normalizer ───────────────────────

@pytest.mark.asyncio
async def test_generate_guess_strips_prefix_and_punct():
    mp = MarvinPlayer.__new__(MarvinPlayer)
    mp._last_buzzed_clue_round = None
    groq = _fake_groq("我猜是巨石強森。")
    with patch("game.marvin_player.get_groq_client", return_value=groq):
        out = await mp.generate_guess(clue_round=4, clues=["明星"], char_count=4, wrong_guesses=[])
    assert out == "巨石強森"


@pytest.mark.asyncio
async def test_generate_guess_preserves_clean_output():
    mp = MarvinPlayer.__new__(MarvinPlayer)
    mp._last_buzzed_clue_round = None
    groq = _fake_groq("黑洞")
    with patch("game.marvin_player.get_groq_client", return_value=groq):
        out = await mp.generate_guess(clue_round=1, clues=[], char_count=2, wrong_guesses=[])
    assert out == "黑洞"

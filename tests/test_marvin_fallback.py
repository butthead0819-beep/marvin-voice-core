"""TDD — MarvinPlayer Cerebras → Groq → Gemini fallback.

Backstory: Marvin uses Groq exclusively. If Groq has an outage, Marvin
breaks every game path (setter, guesser, theme). GameLLMEngine has a
3-layer fallback — MarvinPlayer should too, using the same shared client
builders so the keys/models stay in one place.

Tests:
  A) Groq works → Marvin uses Groq result (no Cerebras/Gemini calls)
  B) Groq raises → Cerebras succeeds → Marvin uses Cerebras result
  C) Both Groq + Cerebras raise → Gemini succeeds → Marvin uses Gemini
  D) All three raise → return a safe fallback string, no crash
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from game.marvin_player import MarvinPlayer


def _fake_chat_response(text: str):
    r = MagicMock()
    r.choices = [MagicMock()]
    r.choices[0].message.content = text
    return r


def _fake_groq(text: str | None = None, *, raises: Exception | None = None):
    c = MagicMock()
    if raises is not None:
        c.chat.completions.create = AsyncMock(side_effect=raises)
    else:
        c.chat.completions.create = AsyncMock(return_value=_fake_chat_response(text or ""))
    return c


def _fake_cerebras(text: str | None = None, *, raises: Exception | None = None):
    return _fake_groq(text, raises=raises)


def _fake_gemini(text: str | None = None, *, raises: Exception | None = None):
    c = MagicMock()
    if raises is not None:
        c.aio.models.generate_content = AsyncMock(side_effect=raises)
    else:
        resp = MagicMock()
        resp.text = text or ""
        c.aio.models.generate_content = AsyncMock(return_value=resp)
    return c


def _make_player():
    mp = MarvinPlayer.__new__(MarvinPlayer)
    mp._last_buzzed_clue_round = None
    return mp


# ── A: Groq happy path ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_groq_first_no_fallback_called():
    mp = _make_player()
    groq = _fake_groq("黑洞")
    cerebras = _fake_cerebras("不應該被呼叫")
    gemini = _fake_gemini("也不應該被呼叫")

    with patch("game.marvin_player.get_groq_client", return_value=groq), \
         patch("game.marvin_player.get_cerebras_client", return_value=cerebras), \
         patch("game.marvin_player.get_gemini_client", return_value=gemini):
        out = await mp.generate_guess(clue_round=1, clues=[], char_count=2, wrong_guesses=[])

    assert out == "黑洞"
    cerebras.chat.completions.create.assert_not_called()
    gemini.aio.models.generate_content.assert_not_called()


# ── B: Groq down → Cerebras takes over ────────────────────────────────────

@pytest.mark.asyncio
async def test_groq_down_falls_to_cerebras():
    mp = _make_player()
    groq = _fake_groq(raises=RuntimeError("groq down"))
    cerebras = _fake_cerebras("巨石強森")
    gemini = _fake_gemini("not reached")

    with patch("game.marvin_player.get_groq_client", return_value=groq), \
         patch("game.marvin_player.get_cerebras_client", return_value=cerebras), \
         patch("game.marvin_player.get_gemini_client", return_value=gemini):
        out = await mp.generate_guess(clue_round=4, clues=["明星"], char_count=4, wrong_guesses=[])

    assert out == "巨石強森"
    gemini.aio.models.generate_content.assert_not_called()


# ── C: Groq + Cerebras down → Gemini takes over ──────────────────────────

@pytest.mark.asyncio
async def test_groq_cerebras_down_falls_to_gemini():
    mp = _make_player()
    groq = _fake_groq(raises=RuntimeError("groq down"))
    cerebras = _fake_cerebras(raises=RuntimeError("cerebras down"))
    gemini = _fake_gemini("周杰倫")

    with patch("game.marvin_player.get_groq_client", return_value=groq), \
         patch("game.marvin_player.get_cerebras_client", return_value=cerebras), \
         patch("game.marvin_player.get_gemini_client", return_value=gemini):
        out = await mp.generate_guess(clue_round=4, clues=["歌手"], char_count=3, wrong_guesses=[])

    assert out == "周杰倫"


# ── D: All three down → safe fallback ─────────────────────────────────────

@pytest.mark.asyncio
async def test_all_providers_down_safe_fallback():
    mp = _make_player()
    groq = _fake_groq(raises=RuntimeError("down"))
    cerebras = _fake_cerebras(raises=RuntimeError("down"))
    gemini = _fake_gemini(raises=RuntimeError("down"))

    with patch("game.marvin_player.get_groq_client", return_value=groq), \
         patch("game.marvin_player.get_cerebras_client", return_value=cerebras), \
         patch("game.marvin_player.get_gemini_client", return_value=gemini):
        out = await mp.generate_guess(clue_round=1, clues=[], char_count=2, wrong_guesses=[])

    # Should return a valid 2-char Chinese fallback, not crash, not empty
    assert isinstance(out, str)
    assert len(out) >= 1


# ── E: generate_setter_answer also uses fallback ─────────────────────────

@pytest.mark.asyncio
async def test_setter_answer_falls_back_when_groq_down():
    mp = _make_player()
    groq = _fake_groq(raises=RuntimeError("groq down"))
    cerebras = _fake_cerebras("拉麵")
    gemini = _fake_gemini("not reached")

    with patch("game.marvin_player.get_groq_client", return_value=groq), \
         patch("game.marvin_player.get_cerebras_client", return_value=cerebras), \
         patch("game.marvin_player.get_gemini_client", return_value=gemini):
        out = await mp.generate_setter_answer("美食", min_len=2, max_len=5)

    assert out == "拉麵"

"""
TDD for Marvin guess quality improvements:
1. No consecutive buzzing — halved probability after buzzing last clue round
2. wrong_guesses always passed (not just round >= 4)
3. Blind-mode prompt (rounds 1-3) includes avoid_line when wrong_guesses exist
4. Last-buzzed-round state updated when Marvin successfully buzzes
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_marvin():
    with patch("game.marvin_player.AsyncOpenAI"):
        from game.marvin_player import MarvinPlayer
        return MarvinPlayer(router=None)


# ── 1. Consecutive buzz — halved probability ──────────────────────────────────

async def _count_buzzes(mp, clue_round: int, trials: int) -> int:
    count = 0
    for _ in range(trials):
        if await mp.should_buzz(clue_round):
            count += 1
    return count


@pytest.mark.asyncio
async def test_consecutive_buzz_reduces_probability():
    """After buzzing in round N, should_buzz in round N+1 has lower probability."""
    mp = _make_marvin()
    mp._last_buzzed_clue_round = 2  # Marvin just buzzed in round 2

    # For round 3, base prob=0.50 → consecutive penalty halves to 0.25
    buzzes = await _count_buzzes(mp, clue_round=3, trials=200)
    base_expected = 200 * 0.50  # 100
    assert buzzes < base_expected * 0.75, (
        f"consecutive buzz should reduce prob; got {buzzes}/200 (base ~{base_expected:.0f})"
    )


@pytest.mark.asyncio
async def test_no_consecutive_penalty_when_skipped():
    """If Marvin didn't buzz last round, normal probability applies."""
    mp = _make_marvin()
    mp._last_buzzed_clue_round = 1  # buzzed in round 1, now evaluating round 3 (not consecutive)

    buzzes = await _count_buzzes(mp, clue_round=3, trials=200)
    base_expected = 200 * 0.50  # 100
    assert buzzes > base_expected * 0.40, (
        f"non-consecutive round should use normal prob; got {buzzes}/200"
    )


@pytest.mark.asyncio
async def test_no_consecutive_penalty_at_round1():
    """Round 1 never gets consecutive penalty (it's always the start)."""
    mp = _make_marvin()
    mp._last_buzzed_clue_round = 0  # impossible value — edge case

    buzzes = await _count_buzzes(mp, clue_round=1, trials=500)
    # Base 10%, should not be near 5%
    assert 20 <= buzzes <= 80, f"round 1 should use ~10% prob, got {buzzes}/500"


# ── 2. _last_buzzed_clue_round updated when buzz succeeds ─────────────────────

@pytest.mark.asyncio
async def test_last_buzzed_round_updated_after_think_then_buzz():
    """think_then_buzz should update _last_buzzed_clue_round when on_buzz_ready fires."""
    mp = _make_marvin()
    mp._weak_client = MagicMock()
    resp = MagicMock()
    resp.choices[0].message.content = "測試答案"
    mp._weak_client.chat.completions.create = AsyncMock(return_value=resp)

    fired = []
    async def on_buzz_ready(guess: str):
        fired.append(guess)

    # Force should_buzz to always return True
    mp.should_buzz = AsyncMock(return_value=True)

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await mp.think_then_buzz(
            clue_round=2, clues=[], char_count=3,
            wrong_guesses=[], on_buzz_ready=on_buzz_ready,
        )

    assert mp._last_buzzed_clue_round == 2, (
        f"_last_buzzed_clue_round should be 2 after buzzing, got {mp._last_buzzed_clue_round}"
    )


@pytest.mark.asyncio
async def test_last_buzzed_round_not_updated_when_no_buzz():
    """If Marvin decides not to buzz, _last_buzzed_clue_round is unchanged."""
    mp = _make_marvin()
    mp._last_buzzed_clue_round = 1
    mp.should_buzz = AsyncMock(return_value=False)

    async def on_buzz_ready(guess: str):
        pass

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await mp.think_then_buzz(
            clue_round=2, clues=[], char_count=3,
            wrong_guesses=[], on_buzz_ready=on_buzz_ready,
        )

    assert mp._last_buzzed_clue_round == 1, "should not update if Marvin didn't buzz"


# ── 3. Blind-mode prompt includes avoid_line ──────────────────────────────────

@pytest.mark.asyncio
async def test_blind_mode_prompt_includes_wrong_guesses():
    """generate_guess in rounds 1-3 should include already-tried answers to avoid."""
    mp = _make_marvin()
    mp._weak_client = MagicMock()
    resp = MagicMock()
    resp.choices[0].message.content = "新答案"
    create_mock = AsyncMock(return_value=resp)
    mp._weak_client.chat.completions.create = create_mock

    await mp.generate_guess(
        clue_round=2, clues=[], char_count=3,
        wrong_guesses=["錯誤一", "錯誤二"],
    )

    call_args = create_mock.call_args
    messages = call_args.kwargs.get("messages") or call_args.args[0] if call_args.args else []
    if not messages:
        messages = call_args[1].get("messages", [])
    user_msg = next((m["content"] for m in messages if m["role"] == "user"), "")
    assert "錯誤一" in user_msg, (
        f"blind-mode prompt should include wrong guesses to avoid, got: {user_msg!r}"
    )
    assert "錯誤二" in user_msg


@pytest.mark.asyncio
async def test_blind_mode_prompt_without_wrong_guesses_unchanged():
    """generate_guess round 1-3 with empty wrong_guesses should not add avoid_line."""
    mp = _make_marvin()
    mp._weak_client = MagicMock()
    resp = MagicMock()
    resp.choices[0].message.content = "答案"
    create_mock = AsyncMock(return_value=resp)
    mp._weak_client.chat.completions.create = create_mock

    await mp.generate_guess(clue_round=1, clues=[], char_count=3, wrong_guesses=[])

    call_args = create_mock.call_args
    messages = call_args.kwargs.get("messages") or []
    user_msg = next((m["content"] for m in messages if m["role"] == "user"), "")
    assert "已猜過" not in user_msg, "no avoid_line when wrong_guesses is empty"


# ── 4. game_cog always passes wrong_guesses ───────────────────────────────────

def test_marvin_guess_task_passes_wrong_guesses_in_early_rounds():
    """_marvin_guess_task should pass wrong_guesses for ALL clue rounds, not just >= 4."""
    import inspect
    import cogs.game_cog as gc_mod
    src = inspect.getsource(gc_mod.BustedCog._marvin_guess_task)
    # Old code: `if clue_round >= 4 else []` — should no longer exist
    assert "clue_round >= 4" not in src, (
        "_marvin_guess_task should not restrict wrong_guesses to round >= 4"
    )

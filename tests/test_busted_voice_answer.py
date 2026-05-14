"""TDD tests for:
1. BUZZ_LOCK_SECONDS 25 → 50 (語音回答時間延長)
2. 第5輪全員已輸入後立即結束倒數
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from game.session import GameSession, GameState, PlayerState
from game.engine import GameEngine


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_engine(players=None):
    session = GameSession(session_id="t1", guild_id=1, channel_id=1)
    eng = GameEngine(session, on_state_change=AsyncMock(), db_path=":memory:")
    if players:
        for uid, name in players:
            session.players.append(PlayerState(user_id=uid, display_name=name))
    return eng


def _make_cog_with_engine(session: GameSession, engine):
    import discord
    from cogs.game_cog import BustedCog
    bot = MagicMock()
    bot.cogs.get.return_value = None
    bot.voice_clients = []
    cog = BustedCog(bot)
    cog._session = session
    cog._engine = engine
    cog._channel = AsyncMock(spec=discord.TextChannel)
    cog._game_state = GameState.CLUE_ACTIVE
    return cog


# ── 1. BUZZ_LOCK_SECONDS = 50 ─────────────────────────────────────────────────

def test_buzz_lock_seconds_is_50():
    """語音回答時間需要 50 秒，不是 25 秒。"""
    from game.engine import BUZZ_LOCK_SECONDS
    assert BUZZ_LOCK_SECONDS == 50.0, f"expected 50.0, got {BUZZ_LOCK_SECONDS}"


@pytest.mark.asyncio
async def test_buzz_in_sets_locked_until_50_seconds():
    """buzz_in 後 buzz_locked_until 應為 now + 50s。"""
    import time
    session = GameSession(session_id="t1", guild_id=1, channel_id=1)
    session.players = [
        PlayerState(user_id="setter", display_name="Alice"),
        PlayerState(user_id="guesser", display_name="Bob"),
    ]
    session.state = GameState.CLUE_ACTIVE
    session.current_setter_id = "setter"
    engine = GameEngine(session, on_state_change=AsyncMock(), db_path=":memory:")

    before = time.time()
    await engine.buzz_in("guesser")

    locked_for = session.buzz_locked_until - before
    assert 49.0 <= locked_for <= 52.0, f"expected ~50s, got {locked_for:.1f}s"


# ── 2. round5_all_submitted() ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_round5_all_submitted_false_when_no_one_submitted():
    engine = _make_engine([("setter", "A"), ("g1", "B"), ("g2", "C")])
    engine.session.current_setter_id = "setter"
    assert engine.round5_all_submitted() is False


@pytest.mark.asyncio
async def test_round5_all_submitted_false_when_only_partial():
    engine = _make_engine([("setter", "A"), ("g1", "B"), ("g2", "C")])
    engine.session.current_setter_id = "setter"
    engine._round5_scores["g1"] = 50
    assert engine.round5_all_submitted() is False


@pytest.mark.asyncio
async def test_round5_all_submitted_true_when_all_guessers_submitted():
    engine = _make_engine([("setter", "A"), ("g1", "B"), ("g2", "C")])
    engine.session.current_setter_id = "setter"
    engine._round5_scores["g1"] = 50
    engine._round5_scores["g2"] = 0
    assert engine.round5_all_submitted() is True


@pytest.mark.asyncio
async def test_round5_all_submitted_ignores_setter():
    """setter 不需要提交，只計算其他人。"""
    engine = _make_engine([("setter", "A"), ("g1", "B")])
    engine.session.current_setter_id = "setter"
    engine._round5_scores["g1"] = 0
    assert engine.round5_all_submitted() is True


@pytest.mark.asyncio
async def test_round5_all_submitted_false_with_no_guessers():
    """如果只有 setter 一人（邊緣情況），回傳 False 避免空局立刻結束。"""
    engine = _make_engine([("setter", "A")])
    engine.session.current_setter_id = "setter"
    assert engine.round5_all_submitted() is False


# ── 3. Cog：全員提交後 deadline 歸零（強制 _clue_loop 立即觸發）────────────────

@pytest.mark.asyncio
async def test_clue_deadline_forced_to_now_when_all_r5_submitted():
    """當 round5_all_submitted()==True，on_state_change 應讓 _clue_deadline <= now。"""
    import time
    from cogs.game_cog import BustedCog

    session = GameSession(session_id="t1", guild_id=1, channel_id=1)
    session.state = GameState.CLUE_ACTIVE
    session.current_round = 5
    session.current_setter_id = "setter"
    session.current_answer = "答案"
    session.players = [
        PlayerState(user_id="setter", display_name="A"),
        PlayerState(user_id="g1", display_name="B"),
    ]

    engine = AsyncMock()
    engine.session = session
    engine.round5_all_submitted = MagicMock(return_value=True)

    cog = _make_cog_with_engine(session, engine)
    cog._game_state = GameState.CLUE_ACTIVE  # prev state = CLUE_ACTIVE (re-notify)
    cog._clue_deadline = time.time() + 75.0   # starts far in the future

    with patch.object(cog, '_post_game_message', new_callable=AsyncMock):
        await cog.on_state_change(session)

    assert cog._clue_deadline <= time.time() + 1.0, \
        "deadline should be set to now (or past) so _clue_loop fires immediately"


@pytest.mark.asyncio
async def test_clue_deadline_not_forced_when_not_all_submitted():
    """只有部分玩家提交時，deadline 不應被強制歸零。"""
    import time
    from cogs.game_cog import BustedCog

    session = GameSession(session_id="t1", guild_id=1, channel_id=1)
    session.state = GameState.CLUE_ACTIVE
    session.current_round = 5
    session.current_setter_id = "setter"
    session.current_answer = "答案"
    session.players = [
        PlayerState(user_id="setter", display_name="A"),
        PlayerState(user_id="g1", display_name="B"),
        PlayerState(user_id="g2", display_name="C"),
    ]

    engine = AsyncMock()
    engine.session = session
    engine.round5_all_submitted = MagicMock(return_value=False)

    cog = _make_cog_with_engine(session, engine)
    cog._game_state = GameState.CLUE_ACTIVE
    future_deadline = time.time() + 75.0
    cog._clue_deadline = future_deadline

    with patch.object(cog, '_post_game_message', new_callable=AsyncMock):
        await cog.on_state_change(session)

    # deadline 應該還是設在未來（被重設為 +75s，不是歸零）
    assert cog._clue_deadline > time.time() + 10.0, \
        "deadline should remain in the future when not all players have submitted"

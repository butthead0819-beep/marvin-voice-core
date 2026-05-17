"""TDD for 6 Busted improvements:
1. 線索計時 75s → 50s
2. Marvin 出題用 LLM + 字數限制合乎題目
3. 猜錯提示幾個字 (position-independent)
4. partial_score 改為位置無關
5. 投票跳過線索
6. Round 5 立刻顯示幾個字, submit 回傳 dict
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from game.session import GameSession, GameState, PlayerState
from game.engine import GameEngine


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_engine_session(players=None, setter_id="setter"):
    session = GameSession(session_id="t1", guild_id=1, channel_id=1)
    session.state = GameState.CLUE_ACTIVE
    session.current_setter_id = setter_id
    session.current_answer = "蘋果汁"
    session.current_round = 1
    if players:
        for uid, name in players:
            session.players.append(PlayerState(user_id=uid, display_name=name))
    return session


def _make_cog(session=None, engine=None):
    from cogs.game_cog import BustedCog
    bot = MagicMock()
    bot.cogs.get.return_value = None
    bot.voice_clients = []
    cog = BustedCog(bot)
    if session:
        cog._session = session
    if engine:
        cog._engine = engine
    cog._channel = AsyncMock(spec=discord.TextChannel)
    cog._game_state = GameState.CLUE_ACTIVE
    return cog


# ═══════════════════════════════════════════════════════════════════
# 1. 線索計時 50s
# ═══════════════════════════════════════════════════════════════════

def test_clue_deadline_is_50s():
    """on_state_change 中的線索倒數應為 50 秒，不是 75 秒。"""
    import inspect
    import cogs.game_cog as gc_mod
    src = inspect.getsource(gc_mod.BustedCog.on_state_change)
    assert "50.0" in src, "clue deadline should be 50.0 s"
    assert "75.0" not in src, "old 75.0 s deadline should be removed"


def test_timer_for_clue_active_is_50():
    """_timer_for_state(CLUE_ACTIVE) 應回傳 50。"""
    from cogs.game_cog import BustedCog
    bot = MagicMock()
    bot.cogs.get.return_value = None
    bot.voice_clients = []
    cog = BustedCog(bot)
    assert cog._timer_for_state(GameState.CLUE_ACTIVE) == 50


# ═══════════════════════════════════════════════════════════════════
# 2. Marvin 出題：LLM generate_setter_answer + 字數限制
# ═══════════════════════════════════════════════════════════════════

def _fake_groq(content: str):
    """Mock for game.llm_clients.get_groq_client — provides chat.completions.create."""
    c = MagicMock()
    resp = MagicMock()
    resp.choices[0].message.content = content
    c.chat.completions.create = AsyncMock(return_value=resp)
    return c


@pytest.mark.asyncio
async def test_marvin_player_has_generate_setter_answer():
    """MarvinPlayer 必須有 generate_setter_answer(theme, min_len, max_len) 方法。"""
    from game.marvin_player import MarvinPlayer
    mp = MarvinPlayer(router=None)
    assert hasattr(mp, "generate_setter_answer"), "MarvinPlayer.generate_setter_answer missing"
    assert callable(mp.generate_setter_answer)


@pytest.mark.asyncio
async def test_generate_setter_answer_respects_max_len():
    """generate_setter_answer 必須確保答案長度 <= max_len。"""
    from game.marvin_player import MarvinPlayer
    mp = MarvinPlayer(router=None)
    groq = _fake_groq("超長答案文字")  # 6 chars > max 5
    with patch("game.marvin_player.get_groq_client", return_value=groq):
        result = await mp.generate_setter_answer("吉他", min_len=2, max_len=5)
    assert len(result) <= 5, f"answer len {len(result)} > max_len 5"


@pytest.mark.asyncio
async def test_generate_setter_answer_respects_min_len():
    """generate_setter_answer 若回傳太短，使用 fallback。"""
    from game.marvin_player import MarvinPlayer
    mp = MarvinPlayer(router=None)
    groq = _fake_groq("一")  # 1 char < min 2
    with patch("game.marvin_player.get_groq_client", return_value=groq):
        result = await mp.generate_setter_answer("吉他", min_len=2, max_len=5)
    assert len(result) >= 2, f"answer len {len(result)} < min_len 2"


@pytest.mark.asyncio
async def test_marvin_setter_task_uses_generate_setter_answer():
    """_marvin_setter_task 應呼叫 marvin.generate_setter_answer，不是舊的 pick()。"""
    from cogs.game_cog import BustedCog
    session = GameSession(session_id="t1", guild_id=1, channel_id=1)
    session.state = GameState.SETTER_INPUT
    session.current_setter_id = "marvin"
    session.current_theme = "吉他"

    engine = AsyncMock()
    engine.session = session

    marvin = MagicMock()
    marvin.generate_setter_answer = AsyncMock(return_value="電吉他")
    marvin.setter_quip = MagicMock(return_value="我來出題")

    cog = _make_cog(session, engine)
    cog._marvin = marvin

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await cog._marvin_setter_task()

    marvin.generate_setter_answer.assert_called_once()
    call_args = marvin.generate_setter_answer.call_args
    # First arg (or kwarg 'theme') should be the session theme
    theme_arg = call_args.args[0] if call_args.args else call_args.kwargs.get("theme")
    assert theme_arg == "吉他", f"should pass theme '吉他', got {theme_arg!r}"


# ═══════════════════════════════════════════════════════════════════
# 3. 猜錯提示幾個字 — submit_answer wrong 回傳 matched_chars
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_submit_answer_wrong_returns_matched_chars():
    """submit_answer 猜錯時，回傳值應包含 matched_chars 和 answer_len。"""
    engine = GameEngine(
        GameSession(session_id="t1", guild_id=1, channel_id=1),
        on_state_change=AsyncMock(),
        db_path=":memory:",
        judge_fn=AsyncMock(return_value=False),
    )
    session = engine.session
    session.players = [
        PlayerState(user_id="setter", display_name="A"),
        PlayerState(user_id="g1", display_name="B"),
    ]
    session.state = GameState.CLUE_ACTIVE
    session.current_setter_id = "setter"
    await engine.set_answer("蘋果汁")
    await engine.buzz_in("g1")

    result = await engine.submit_answer("g1", "西瓜汁")  # 汁 matches
    assert result["correct"] is False
    assert "matched_chars" in result, "result must have 'matched_chars' key"
    assert result["matched_chars"] == 1  # 汁 is in 蘋果汁


@pytest.mark.asyncio
async def test_submit_answer_wrong_matched_chars_position_independent():
    """matched_chars 比對不看位置。"""
    engine = GameEngine(
        GameSession(session_id="t1", guild_id=1, channel_id=1),
        on_state_change=AsyncMock(),
        db_path=":memory:",
        judge_fn=AsyncMock(return_value=False),
    )
    session = engine.session
    session.players = [
        PlayerState(user_id="setter", display_name="A"),
        PlayerState(user_id="g1", display_name="B"),
    ]
    session.state = GameState.CLUE_ACTIVE
    session.current_setter_id = "setter"
    await engine.set_answer("蘋果汁")
    await engine.buzz_in("g1")

    # 汁蘋水 → 蘋 and 汁 appear in 蘋果汁, regardless of position
    result = await engine.submit_answer("g1", "汁蘋水")
    assert result["matched_chars"] == 2


# ═══════════════════════════════════════════════════════════════════
# 4. partial_score 位置無關
# ═══════════════════════════════════════════════════════════════════

def test_partial_score_position_independent():
    """partial_score("蘋果汁", "汁果蘋") 應為 100（全部字都在，只是順序不同）。"""
    from game.scoring import partial_score
    assert partial_score("蘋果汁", "汁果蘋") == 100


def test_partial_score_counts_chars_not_positions():
    """reversed answer should score as high as correct-order answer."""
    from game.scoring import partial_score
    assert partial_score("蘋果汁", "汁蘋水") == 66  # 蘋+汁 in guess


def test_partial_score_zero_when_no_chars_match():
    from game.scoring import partial_score
    assert partial_score("蘋果汁", "大西瓜") == 0


def test_partial_score_empty_answer():
    from game.scoring import partial_score
    assert partial_score("", "anything") == 0


# ═══════════════════════════════════════════════════════════════════
# 5. 投票跳過線索
# ═══════════════════════════════════════════════════════════════════

def test_busted_cog_has_skip_votes_attr():
    """BustedCog 必須有 _skip_votes 屬性（set）。"""
    from cogs.game_cog import BustedCog
    bot = MagicMock()
    bot.cogs.get.return_value = None
    bot.voice_clients = []
    cog = BustedCog(bot)
    assert hasattr(cog, "_skip_votes")
    assert isinstance(cog._skip_votes, set)


def test_buzz_view_has_skip_button():
    """BuzzView 必須包含一個跳過線索的按鈕。"""
    from cogs.game_cog import BuzzView
    cog = MagicMock()
    view = BuzzView(cog, disabled=False)
    labels = [item.label for item in view.children if hasattr(item, "label")]
    # Should have at least 2 buttons: buzz + skip
    assert len(view.children) >= 2, f"BuzzView should have >=2 buttons, got {len(view.children)}"
    skip_btn = next((item for item in view.children
                     if hasattr(item, "label") and "跳過" in (item.label or "")), None)
    assert skip_btn is not None, "BuzzView must have a '跳過' button"


@pytest.mark.asyncio
async def test_skip_vote_advances_clue_when_all_vote():
    """當所有非 setter 人類玩家都投票跳過，應立刻 advance_clue。"""
    from cogs.game_cog import BustedCog

    session = GameSession(session_id="t1", guild_id=1, channel_id=1)
    session.state = GameState.CLUE_ACTIVE
    session.current_setter_id = "setter"
    session.current_answer = "蘋果汁"
    session.current_round = 1
    session.players = [
        PlayerState(user_id="setter", display_name="Setter"),
        PlayerState(user_id="g1", display_name="G1"),
        PlayerState(user_id="g2", display_name="G2"),
    ]

    engine = AsyncMock()
    engine.session = session

    cog = _make_cog(session, engine)
    cog._skip_votes = set()

    # Simulate both guessers voting
    await cog.record_skip_vote("g1")
    await cog.record_skip_vote("g2")

    engine.advance_clue.assert_called_once()


@pytest.mark.asyncio
async def test_skip_vote_does_not_advance_when_partial():
    """只有部分玩家投票，不應 advance_clue。"""
    from cogs.game_cog import BustedCog

    session = GameSession(session_id="t1", guild_id=1, channel_id=1)
    session.state = GameState.CLUE_ACTIVE
    session.current_setter_id = "setter"
    session.current_answer = "蘋果汁"
    session.current_round = 1
    session.players = [
        PlayerState(user_id="setter", display_name="Setter"),
        PlayerState(user_id="g1", display_name="G1"),
        PlayerState(user_id="g2", display_name="G2"),
    ]

    engine = AsyncMock()
    engine.session = session

    cog = _make_cog(session, engine)
    cog._skip_votes = set()

    await cog.record_skip_vote("g1")  # only 1 of 2

    engine.advance_clue.assert_not_called()


@pytest.mark.asyncio
async def test_skip_vote_ignores_setter_and_marvin():
    """Setter 和 Marvin 的投票不應計入。"""
    from cogs.game_cog import BustedCog

    session = GameSession(session_id="t1", guild_id=1, channel_id=1)
    session.state = GameState.CLUE_ACTIVE
    session.current_setter_id = "setter"
    session.current_answer = "蘋果汁"
    session.current_round = 1
    session.players = [
        PlayerState(user_id="setter", display_name="Setter"),
        PlayerState(user_id="marvin", display_name="Marvin"),
        PlayerState(user_id="g1", display_name="G1"),
    ]

    engine = AsyncMock()
    engine.session = session

    cog = _make_cog(session, engine)
    cog._skip_votes = set()

    # Only g1 is the eligible voter; one vote = all voted
    await cog.record_skip_vote("g1")

    engine.advance_clue.assert_called_once()


# ═══════════════════════════════════════════════════════════════════
# 6. submit_round5_answer 回傳 dict + matched count
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_submit_round5_answer_returns_dict():
    """submit_round5_answer 應回傳 dict，包含 pts、matched、answer_len。"""
    engine = GameEngine(
        GameSession(session_id="t1", guild_id=1, channel_id=1),
        on_state_change=AsyncMock(),
        db_path=":memory:",
    )
    session = engine.session
    session.players = [
        PlayerState(user_id="setter", display_name="A"),
        PlayerState(user_id="g1", display_name="B"),
    ]
    session.state = GameState.CLUE_ACTIVE
    session.current_setter_id = "setter"
    await engine.set_answer("蘋果汁")
    engine.session.current_round = 5  # set_answer resets to 1; override after

    result = await engine.submit_round5_answer("g1", "汁蘋水")  # 蘋+汁 match

    assert isinstance(result, dict), f"expected dict, got {type(result)}"
    assert "pts" in result
    assert "matched" in result
    assert "answer_len" in result
    assert result["matched"] == 2      # 蘋 and 汁 appear in 蘋果汁
    assert result["answer_len"] == 3   # 蘋果汁 = 3 chars


@pytest.mark.asyncio
async def test_submit_round5_answer_score_is_proportion():
    """pts 應等於 int(100 * matched / answer_len)。"""
    engine = GameEngine(
        GameSession(session_id="t1", guild_id=1, channel_id=1),
        on_state_change=AsyncMock(),
        db_path=":memory:",
    )
    session = engine.session
    session.players = [
        PlayerState(user_id="setter", display_name="A"),
        PlayerState(user_id="g1", display_name="B"),
    ]
    session.state = GameState.CLUE_ACTIVE
    session.current_setter_id = "setter"
    await engine.set_answer("蘋果汁")
    engine.session.current_round = 5  # set_answer resets to 1; override after

    result = await engine.submit_round5_answer("g1", "汁蘋水")
    expected_pts = int(100 * result["matched"] / result["answer_len"])
    assert result["pts"] == expected_pts

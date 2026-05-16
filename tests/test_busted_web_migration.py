"""TDD — Busted 遊戲 Web 遷移

驗項：
A) BustedCog 有 _ws_hub 和 _player_tokens 屬性
B) _build_ws_state 涵蓋所有 GameState 關鍵欄位
C) BUSTED_DISCORD_SILENT=true 讓 _post_game_message 跳過 Discord
D) _handle_web_action 路由 b_buzz → engine.buzz_in
E) _handle_web_action 路由 b_answer → engine.submit_answer
F) _handle_web_action 路由 b_skip_vote → record_skip_vote
G) _handle_web_action 路由 b_set_answer → engine.set_answer
H) _handle_web_action 路由 b_theme_select → engine.select_theme
I) _handle_web_action 路由 b_round5_answer → engine.submit_round5_answer
J) on_state_change 呼叫 _emit_ws_state
K) token 系統：_generate_player_token / resolve_token / _build_player_link
"""

from __future__ import annotations

import os
import uuid
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_bot():
    bot = MagicMock()
    bot.cogs.get.return_value = None
    bot.voice_clients = []
    return bot


def _make_cog():
    from cogs.game_cog import BustedCog
    return BustedCog(_make_bot())


def _make_session(state):
    from game.session import GameSession, GameState, PlayerState
    s = GameSession.__new__(GameSession)
    s.session_id = "test"
    s.guild_id = 1
    s.channel_id = 1
    s.state = state
    s.players = [
        PlayerState(user_id="u1", display_name="狗與露", score=0),
        PlayerState(user_id="marvin", display_name="Marvin", score=0),
    ]
    s.current_setter_id = "u1"
    s.current_round = 2
    s.current_answer = "蘋果汁"
    s.current_clues = ["它是液體", "可以喝"]
    s.candidate_themes = ["食物", "動物", "電影"]
    s.current_theme = "食物"
    s.buzz_holder_id = None
    s.wrong_guesses = []
    s.game_message_id = None
    s.setter_hint = None
    s.applied_hint = None
    s.round5_scores = {}
    return s


# ── A: 屬性 ──────────────────────────────────────────────────────────────────

def test_bustedcog_has_ws_hub():
    cog = _make_cog()
    assert hasattr(cog, "_ws_hub"), "BustedCog 必須有 _ws_hub 屬性"

def test_bustedcog_has_player_tokens():
    cog = _make_cog()
    assert hasattr(cog, "_player_tokens"), "BustedCog 必須有 _player_tokens 屬性"
    assert isinstance(cog._player_tokens, dict)


# ── B: _build_ws_state 各 state 欄位 ─────────────────────────────────────────

def test_build_ws_state_joining():
    from game.session import GameState
    cog = _make_cog()
    s = _make_session(GameState.JOINING)
    ws = cog._build_ws_state(s)
    assert ws["type"] == "game_state"
    assert ws["game"] == "busted"
    assert ws["phase"] == "joining"
    assert "players" in ws
    assert "scores" in ws

def test_build_ws_state_clue_active():
    from game.session import GameState
    cog = _make_cog()
    s = _make_session(GameState.CLUE_ACTIVE)
    ws = cog._build_ws_state(s)
    assert ws["phase"] == "clue_active"
    assert "clues" in ws
    assert "answer_len" in ws
    assert "remaining_sec" in ws
    assert "skip_votes" in ws

def test_build_ws_state_buzz_locked():
    from game.session import GameState
    cog = _make_cog()
    s = _make_session(GameState.BUZZ_LOCKED)
    s.buzz_holder_id = "u1"
    ws = cog._build_ws_state(s)
    assert ws["phase"] == "buzz_locked"
    assert ws["buzz_holder"] == "狗與露"

def test_build_ws_state_theme_select():
    from game.session import GameState
    cog = _make_cog()
    s = _make_session(GameState.THEME_SELECT)
    ws = cog._build_ws_state(s)
    assert ws["phase"] == "theme_select"
    assert "candidate_themes" in ws

def test_build_ws_state_round5():
    from game.session import GameState
    cog = _make_cog()
    s = _make_session(GameState.CLUE_ACTIVE)
    s.current_round = 5
    ws = cog._build_ws_state(s)
    assert ws["is_round5"] is True


# ── C: BUSTED_DISCORD_SILENT 靜音模式 ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_post_game_message_skips_discord_when_silent():
    cog = _make_cog()
    cog._channel = AsyncMock()
    cog._session = MagicMock()
    cog._session.game_message_id = None

    embed = MagicMock()
    with patch.dict(os.environ, {"BUSTED_DISCORD_SILENT": "true"}):
        await cog._post_game_message(embed)

    cog._channel.send.assert_not_called()

@pytest.mark.asyncio
async def test_post_game_message_sends_discord_when_not_silent():
    cog = _make_cog()
    fake_msg = MagicMock()
    fake_msg.id = 42
    cog._channel = AsyncMock()
    cog._channel.send = AsyncMock(return_value=fake_msg)
    cog._session = MagicMock()
    cog._session.game_message_id = None

    embed = MagicMock()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("BUSTED_DISCORD_SILENT", None)
        await cog._post_game_message(embed)

    cog._channel.send.assert_called_once()


# ── D: b_buzz → engine.buzz_in ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_web_action_b_buzz():
    from game.session import GameState
    cog = _make_cog()
    s = _make_session(GameState.CLUE_ACTIVE)
    cog._session = s
    cog._engine = AsyncMock()
    cog._engine.buzz_in = AsyncMock(return_value=False)
    cog._player_tokens = {"tok1": "u1"}

    await cog._handle_web_action({"type": "b_buzz", "resolved_user_id": "u1"})
    cog._engine.buzz_in.assert_called_once_with("u1")


# ── E: b_answer → engine.submit_answer ───────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_web_action_b_answer():
    from game.session import GameState
    cog = _make_cog()
    s = _make_session(GameState.BUZZ_LOCKED)
    s.buzz_holder_id = "u1"
    cog._session = s
    cog._channel = AsyncMock()
    cog._engine = AsyncMock()
    cog._engine.submit_answer = AsyncMock(return_value={"correct": False})

    await cog._handle_web_action({
        "type": "b_answer", "resolved_user_id": "u1", "text": "蘋果"
    })
    cog._engine.submit_answer.assert_called_once_with("u1", "蘋果")


# ── F: b_skip_vote → record_skip_vote ────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_web_action_b_skip_vote():
    from game.session import GameState, PlayerState
    cog = _make_cog()
    s = _make_session(GameState.CLUE_ACTIVE)
    s.current_setter_id = "marvin"  # u1 and u2 are eligible voters
    # add a second human player so 1/2 votes don't trigger auto-advance
    s.players.append(PlayerState(user_id="u2", display_name="玩家二", score=0))
    cog._session = s
    cog._channel = AsyncMock()
    cog._engine = AsyncMock()
    cog._engine.advance_clue = AsyncMock(return_value={})

    await cog._handle_web_action({"type": "b_skip_vote", "resolved_user_id": "u1"})
    assert "u1" in cog._skip_votes


# ── G: b_set_answer → engine.set_answer ──────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_web_action_b_set_answer():
    from game.session import GameState
    cog = _make_cog()
    s = _make_session(GameState.SETTER_INPUT)
    s.current_setter_id = "u1"
    cog._session = s
    cog._engine = AsyncMock()
    cog._engine.set_answer = AsyncMock(return_value=None)

    await cog._handle_web_action({
        "type": "b_set_answer", "resolved_user_id": "u1", "answer": "蘋果汁"
    })
    cog._engine.set_answer.assert_called_once_with("蘋果汁")


# ── H: b_theme_select → engine.select_theme ──────────────────────────────────

@pytest.mark.asyncio
async def test_handle_web_action_b_theme_select():
    from game.session import GameState
    cog = _make_cog()
    s = _make_session(GameState.THEME_SELECT)
    s.current_setter_id = "u1"
    cog._session = s
    cog._engine = AsyncMock()
    cog._engine.select_theme = AsyncMock(return_value=True)

    await cog._handle_web_action({
        "type": "b_theme_select", "resolved_user_id": "u1", "theme": "食物"
    })
    cog._engine.select_theme.assert_called_once_with("食物")


# ── I: b_round5_answer → engine.submit_round5_answer ─────────────────────────

@pytest.mark.asyncio
async def test_handle_web_action_b_round5_answer():
    from game.session import GameState
    cog = _make_cog()
    s = _make_session(GameState.CLUE_ACTIVE)
    s.current_round = 5
    cog._session = s
    cog._channel = AsyncMock()
    cog._engine = AsyncMock()
    cog._engine.submit_round5_answer = AsyncMock(return_value={"pts": 60})

    await cog._handle_web_action({
        "type": "b_round5_answer", "resolved_user_id": "u1", "text": "蘋果汁"
    })
    cog._engine.submit_round5_answer.assert_called_once_with("u1", "蘋果汁")


# ── J: on_state_change 呼叫 _emit_ws_state ────────────────────────────────────

@pytest.mark.asyncio
async def test_on_state_change_calls_emit_ws_state():
    from game.session import GameState
    cog = _make_cog()
    s = _make_session(GameState.JOINING)
    cog._emit_ws_state = AsyncMock()
    cog._emit_phase = AsyncMock()
    cog._channel = AsyncMock()
    cog._channel.send = AsyncMock(return_value=MagicMock(id=1))

    await cog.on_state_change(s)

    cog._emit_ws_state.assert_called_once_with(s)


# ── K: token 系統 ─────────────────────────────────────────────────────────────

def test_bustedcog_generate_token():
    cog = _make_cog()
    tok = cog._generate_player_token("u1")
    assert isinstance(tok, str) and len(tok) > 0
    assert cog._player_tokens[tok] == "u1"

def test_bustedcog_resolve_token():
    cog = _make_cog()
    tok = cog._generate_player_token("u1")
    assert cog.resolve_token(tok) == "u1"
    assert cog.resolve_token("bad") is None

def test_bustedcog_build_player_link():
    cog = _make_cog()
    with patch.dict(os.environ, {"GAME_PUBLIC_URL": "https://test.example.com"}):
        link = cog._build_player_link("u1")
    assert "test.example.com" in link and "token=" in link

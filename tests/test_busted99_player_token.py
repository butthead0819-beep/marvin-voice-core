"""TDD — Busted99 個人連結 token 系統

每個玩家在遊戲開始時收到 Marvin 的 ephemeral 訊息，
內含含 token 的個人連結。瀏覽器以 token 自動識別身分。

驗項：
A) Busted99Cog 有 _player_tokens: dict[str, str]（token → user_id）
B) _generate_player_token(user_id) 回傳不重複的 token，並存入 _player_tokens
C) resolve_token(token) 回傳 user_id；未知 token 回傳 None
D) 每次新遊戲 GUESSING 開始時，_player_tokens 被清空並重新產生
E) _build_player_link(user_id) 回傳含 token 的 URL（用 GAME_PUBLIC_URL env）
F) GameWSHub 收到帶 token 的 action 後，hub 呼叫 action_handler 時 action 含 resolved user_id
"""

from __future__ import annotations

import os
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_cog():
    import cogs.busted99_cog as b99_mod
    bot = MagicMock()
    bot.cogs.get.return_value = None
    bot.voice_clients = []
    return b99_mod.Busted99Cog(bot)


# ── A: _player_tokens 屬性 ────────────────────────────────────────────────────

def test_busted99cog_has_player_tokens():
    cog = _make_cog()
    assert hasattr(cog, "_player_tokens"), "Busted99Cog 必須有 _player_tokens 屬性"
    assert isinstance(cog._player_tokens, dict), "_player_tokens 必須是 dict"


# ── B: _generate_player_token ─────────────────────────────────────────────────

def test_generate_player_token_returns_nonempty_string():
    cog = _make_cog()
    tok = cog._generate_player_token("user123")
    assert isinstance(tok, str) and len(tok) > 0

def test_generate_player_token_stores_in_dict():
    cog = _make_cog()
    tok = cog._generate_player_token("user123")
    assert cog._player_tokens[tok] == "user123"

def test_generate_player_token_unique_per_user():
    cog = _make_cog()
    t1 = cog._generate_player_token("u1")
    t2 = cog._generate_player_token("u2")
    assert t1 != t2


# ── C: resolve_token ──────────────────────────────────────────────────────────

def test_resolve_token_returns_user_id():
    cog = _make_cog()
    tok = cog._generate_player_token("u42")
    assert cog.resolve_token(tok) == "u42"

def test_resolve_token_unknown_returns_none():
    cog = _make_cog()
    assert cog.resolve_token("nosuchtoken") is None


# ── D: 新遊戲清空舊 tokens ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_player_tokens_cleared_on_joining():
    """on_state_change(JOINING) 時 _player_tokens 被重置（新一局）。
    GUESSING 各輪之間 tokens 不應被清空，否則玩家連結失效。
    """
    import cogs.busted99_cog as b99_mod
    from game.busted99.session import Busted99State
    from game.busted99.session import Busted99Session, Player99State

    cog = _make_cog()
    cog._player_tokens = {"stale_token": "old_user"}

    session = Busted99Session.__new__(Busted99Session)
    session.session_id = "t"
    session.state = Busted99State.JOINING
    session.current_guesser_id = None
    session.setter_id = None
    session.players = []
    session.low_bound = 1
    session.high_bound = 99
    session.answer = None
    session.round_num = 1
    session.game_message_id = None
    session.last_guess_result = None

    cog._channel = AsyncMock()
    cog._channel.send = AsyncMock(return_value=MagicMock(id=1))
    cog._emit_phase = AsyncMock()
    cog._emit_ws_state = AsyncMock()

    await cog.on_state_change(session)

    assert "stale_token" not in cog._player_tokens, (
        "on_state_change(JOINING) 必須清空前一局的 _player_tokens"
    )


# ── E: _build_player_link ─────────────────────────────────────────────────────

def test_build_player_link_contains_token():
    cog = _make_cog()
    with patch.dict(os.environ, {"GAME_PUBLIC_URL": "https://test.example.com"}):
        link = cog._build_player_link("u1")
    assert "test.example.com" in link
    assert "token=" in link

def test_build_player_link_falls_back_to_localhost():
    cog = _make_cog()
    env = {k: v for k, v in os.environ.items() if k != "GAME_PUBLIC_URL"}
    with patch.dict(os.environ, env, clear=True):
        link = cog._build_player_link("u1")
    assert "localhost" in link or "127.0.0.1" in link


# ── F: hub 傳遞 resolved user_id 給 action_handler ───────────────────────────

@pytest.mark.asyncio
async def test_hub_resolves_token_in_action():
    """Hub 收到帶 token 的 action 時，呼叫 action_handler 前先 resolve token。"""
    import aiohttp
    from game_ws_hub import GameWSHub

    received = []

    async def handler(action: dict):
        received.append(action)

    hub = GameWSHub(port=18780, action_handler=handler)
    hub.set_token_resolver(lambda tok: "discord_user_99" if tok == "mytoken" else None)
    await hub.start()

    session = aiohttp.ClientSession()
    ws = await session.ws_connect("http://127.0.0.1:18780/game-ws")
    await asyncio.sleep(0.05)

    import json
    await ws.send_str(json.dumps({
        "type": "b99_guess", "token": "mytoken", "number": 55
    }))
    await asyncio.sleep(0.1)

    assert len(received) == 1
    assert received[0].get("resolved_user_id") == "discord_user_99"
    assert received[0]["number"] == 55

    await ws.close()
    await session.close()
    await hub.stop()

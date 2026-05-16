"""TDD — Busted99 猜題 600 秒 + 投票跳過 AFK 猜題者 + 遊戲訊息 edit-in-place

驗項：
A) GUESS_TIMEOUT_SECONDS == 600.0
B) _guesser_timeout_task_coro 睡 600 秒
C) on_state_change GUESSING 的 deadline 是 600
D) Busted99Cog 有 _skip_votes: set[str]
E) SkipVote99View 存在，且有「跳過」按鈕
F) record_skip_vote99 — 過半非猜題人類玩家投票後觸發 force_skip_round
G) record_skip_vote99 — 票數不足時不觸發
H) 猜題者本身的 ID 不被計入投票資格
I) 新的 GUESSING 回合開始時 _skip_votes 被重置
J) Busted99Cog 有 _upsert_game_message（edit-in-place 輔助）
"""

from __future__ import annotations

import inspect
import asyncio
import math
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


# ── 共用 helpers ─────────────────────────────────────────────────────────────

def _make_cog99():
    import cogs.busted99_cog as b99_mod
    bot = MagicMock()
    bot.cogs.get.return_value = None
    bot.voice_clients = []
    return b99_mod.Busted99Cog(bot)


def _make_session(guesser_id: str, other_ids: list[str]):
    """建立一個正在 GUESSING 的輕量 session stub。"""
    from game.busted99.session import Busted99Session, Busted99State, Player99State
    s = Busted99Session.__new__(Busted99Session)
    s.session_id = "test"
    s.state = Busted99State.GUESSING
    s.current_guesser_id = guesser_id
    s.setter_id = other_ids[0] if other_ids else "setter"
    s.players = [Player99State(user_id=guesser_id, display_name="Guesser", score=0)]
    for uid in other_ids:
        s.players.append(Player99State(user_id=uid, display_name=uid, score=0))
    s.low_bound = 1
    s.high_bound = 99
    s.answer = 42
    s.round_num = 1
    s.game_message_id = None
    s.last_guess_result = None
    return s


# ── A: 常數 ──────────────────────────────────────────────────────────────────

def test_guess_timeout_constant_is_600():
    from game.busted99.engine import GUESS_TIMEOUT_SECONDS
    assert GUESS_TIMEOUT_SECONDS == 600.0, (
        f"GUESS_TIMEOUT_SECONDS 應為 600.0，目前是 {GUESS_TIMEOUT_SECONDS}"
    )


# ── B: timeout coro 睡 600 秒 ────────────────────────────────────────────────

def test_guesser_timeout_coro_sleeps_600():
    import cogs.busted99_cog as b99_mod
    src = inspect.getsource(b99_mod.Busted99Cog._guesser_timeout_task_coro)
    assert "600" in src, (
        "_guesser_timeout_task_coro 應 sleep(600)，目前仍是 15 秒"
    )


# ── C: on_state_change deadline 用 600 ───────────────────────────────────────

def test_guessing_deadline_set_to_600():
    import cogs.busted99_cog as b99_mod
    src = inspect.getsource(b99_mod.Busted99Cog.on_state_change)
    assert "600.0" in src, (
        "on_state_change 應將 _guessing_deadline 設為 time.time() + 600.0"
    )


# ── D: _skip_votes 屬性 ──────────────────────────────────────────────────────

def test_busted99cog_has_skip_votes_attr():
    cog = _make_cog99()
    assert hasattr(cog, "_skip_votes"), "Busted99Cog 必須有 _skip_votes 屬性"
    assert isinstance(cog._skip_votes, set), "_skip_votes 必須是 set"


# ── E: SkipVote99View 存在且有跳過按鈕 ────────────────────────────────────────

def test_skip_vote99_view_exists():
    import cogs.busted99_cog as b99_mod
    assert hasattr(b99_mod, "SkipVote99View"), (
        "cogs.busted99_cog 必須定義 SkipVote99View"
    )


def test_skip_vote99_view_has_skip_button():
    import cogs.busted99_cog as b99_mod
    cog = _make_cog99()
    view = b99_mod.SkipVote99View(cog)
    labels = [item.label for item in view.children if hasattr(item, "label")]
    skip_btn = next(
        (item for item in view.children
         if hasattr(item, "label") and "跳過" in (item.label or "")),
        None,
    )
    assert skip_btn is not None, (
        f"SkipVote99View 必須有含「跳過」的按鈕，目前按鈕：{labels}"
    )


# ── F: 過半投票觸發 force_skip_round ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_skip_vote99_majority_triggers_skip():
    """1 guesser + 2 other humans → 2 票 = 全體投 → 觸發 force_skip_round。"""
    import cogs.busted99_cog as b99_mod

    cog = _make_cog99()
    session = _make_session(guesser_id="guesser", other_ids=["p1", "p2"])
    cog._session = session
    cog._skip_votes = set()

    cog.force_skip_round = AsyncMock()

    await cog.record_skip_vote99("p1")
    await cog.record_skip_vote99("p2")

    cog.force_skip_round.assert_called_once()


# ── G: 票數不足不觸發 ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_skip_vote99_partial_no_skip():
    """只有 1/2 non-guesser 投票，不觸發。"""
    import cogs.busted99_cog as b99_mod

    cog = _make_cog99()
    session = _make_session(guesser_id="guesser", other_ids=["p1", "p2"])
    cog._session = session
    cog._skip_votes = set()

    cog.force_skip_round = AsyncMock()

    await cog.record_skip_vote99("p1")  # only 1 of 2

    cog.force_skip_round.assert_not_called()


# ── H: 猜題者投票不計入 ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_skip_vote99_guesser_vote_ignored():
    """猜題者投票自己，不觸發（非資格人）。"""
    import cogs.busted99_cog as b99_mod

    cog = _make_cog99()
    session = _make_session(guesser_id="guesser", other_ids=["p1"])
    cog._session = session
    cog._skip_votes = set()

    cog.force_skip_round = AsyncMock()

    # guesser votes for themselves — should be silently ignored
    await cog.record_skip_vote99("guesser")

    cog.force_skip_round.assert_not_called()


# ── I: 新回合重置 _skip_votes ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_skip_votes_reset_on_new_guessing_round():
    """on_state_change 進入 GUESSING 時，_skip_votes 必須清空。"""
    import cogs.busted99_cog as b99_mod
    from game.busted99.session import Busted99State

    cog = _make_cog99()
    session = _make_session(guesser_id="guesser", other_ids=["p1"])
    session.game_message_id = None
    cog._skip_votes = {"p1", "p2"}  # dirty from previous round

    # stub out everything that on_state_change touches
    cog._channel = AsyncMock()
    cog._channel.send = AsyncMock(return_value=MagicMock(id=99))
    cog._channel.fetch_message = AsyncMock(side_effect=Exception("not found"))
    cog._play_sfx = AsyncMock()
    cog._spawn = MagicMock()
    cog._cancel_guesser_timeout = MagicMock()
    cog._emit_phase = AsyncMock()

    await cog.on_state_change(session)

    assert cog._skip_votes == set(), (
        f"on_state_change(GUESSING) 後 _skip_votes 應為空集合，實際：{cog._skip_votes}"
    )


# ── J: _upsert_game_message 存在 ─────────────────────────────────────────────

def test_busted99cog_has_upsert_game_message():
    cog = _make_cog99()
    assert hasattr(cog, "_upsert_game_message"), (
        "Busted99Cog 必須有 _upsert_game_message 方法（edit-in-place fallback）"
    )
    assert asyncio.iscoroutinefunction(cog._upsert_game_message), (
        "_upsert_game_message 必須是 async 方法"
    )

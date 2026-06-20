"""
Tests for three first-round Busted99 bugs:

Bug A — 錯誤猜題後，_guesser_timeout_task 必須存活（不被提前取消）。
        原本由 receive_voice_answer_by_speaker 觸發；語音停用後改由 on_message 驗證。

Bug B — handle_stt_result line 1715 的 follow-up wake window 沒有 game_mode
        防護：開局前 8 秒若 window 未關閉，玩家喊出的數字會走 LLM 而非遊戲路由。

Bug D — on_state_change GUESSING branch 在 Marvin 猜題時不啟動
        countdown loop 也不啟動 timeout；若 _marvin_guesser_task 失敗，
        遊戲永久卡住（15 秒計時凍結）。
"""
from __future__ import annotations

import asyncio
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_bot():
    bot = MagicMock()
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    return bot


def _make_cog(bot=None):
    if bot is None:
        bot = _make_bot()
    from cogs.busted99_cog import Busted99Cog
    cog = Busted99Cog(bot)
    return cog


async def _bootstrap_guessing(cog, *, human_is_guesser: bool = True):
    """
    Bootstrap a session straight into GUESSING state.

    human_is_guesser=True  → Jack猜  Marvin出題
    human_is_guesser=False → Marvin猜 Jack出題
    """
    from game.busted99.engine import Busted99Engine
    from game.busted99.session import Busted99Session, Busted99State

    session = Busted99Session(
        session_id=str(uuid.uuid4()),
        guild_id=1,
        channel_id=1,
    )
    cog._channel = AsyncMock()
    cog._channel.send = AsyncMock()

    on_state_change_calls = []

    async def _fake_state_change(s):
        on_state_change_calls.append(s.state)
        # 模擬 on_state_change 側效果（不真的 spawn asyncio tasks）
        from game.busted99.session import Busted99State as S
        if s.state == S.GUESSING:
            cog._session = s
            # 在測試中我們不真的 spawn；只記錄
        elif s.state == S.GAME_OVER:
            cog._session = s

    engine = Busted99Engine(
        session=session,
        on_state_change=_fake_state_change,
        db_path=":memory:",
    )
    cog._engine = engine
    cog._session = session

    jack_id = "jack_001"
    if human_is_guesser:
        await engine.add_player("marvin", "Marvin")
        await engine.add_player(jack_id, "狗與露")
        session.setter_id = "marvin"
        session.current_guesser_id = jack_id
    else:
        await engine.add_player(jack_id, "狗與露")
        await engine.add_player("marvin", "Marvin")
        session.setter_id = jack_id
        session.current_guesser_id = "marvin"

    session.answer = 50
    session.low_bound = 1
    session.high_bound = 99
    session.guessing_queue = []
    from game.busted99.session import Busted99State as S
    session.state = S.GUESSING

    return session, engine, jack_id


# ══════════════════════════════════════════════════════════════════════════════
# Bug A — 冗餘 _cancel_guesser_timeout 在 receive_voice_answer_by_speaker
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_bugA_next_guesser_timeout_not_cancelled_after_wrong_guess():
    """
    Wrong guess should NOT cancel the timeout that on_state_change just spawned
    for the next guesser.

    Triggered via on_message (keyboard path) — the old voice path is disabled.
    submit_guess calls on_state_change which spawns a new timeout task; nothing
    in _process_guess should cancel it afterwards.
    """
    from cogs.busted99_cog import Busted99Cog

    bot = _make_bot()
    cog = _make_cog(bot)
    session, engine, jack_id = await _bootstrap_guessing(cog, human_is_guesser=True)

    cog._channel = AsyncMock()
    cog._channel.send = AsyncMock()
    cog._channel.fetch_message = AsyncMock(return_value=AsyncMock())

    spawned_tasks: list[asyncio.Task] = []

    original_spawn = cog._spawn

    def _recording_spawn(coro):
        t = original_spawn(coro)
        spawned_tasks.append(t)
        return t

    cog._spawn = _recording_spawn
    cog._post_game_message = AsyncMock()
    cog._play_sfx = AsyncMock()

    engine._on_state_change = cog.on_state_change

    # Jack types "30" in chat (below secret 50 → wrong_low)
    msg = MagicMock()
    msg.content = "30"
    msg.author = MagicMock()
    msg.author.display_name = "狗與露"
    msg.author.id = jack_id   # str "jack_001" — on_message compares str(author.id)
    msg.author.bot = False
    await cog.on_message(msg)

    # After on_message returns, a NEW timeout task must have been spawned (by
    # on_state_change for the next round) AND must NOT have been cancelled.
    assert cog._guesser_timeout_task is not None, (
        "_guesser_timeout_task should exist for the next guesser's round"
    )
    assert not cog._guesser_timeout_task.cancelled(), (
        "the next guesser's timeout task must not be immediately cancelled"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Bug D — Marvin 猜題時沒有 timeout / countdown
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_bugD_marvin_guesser_still_gets_timeout_task():
    """
    When it is Marvin's turn to guess, a fallback timeout task MUST be spawned.

    Before fix: on_state_change enters the `if session.current_guesser_id == "marvin":`
    branch and only spawns _marvin_guesser_task — no timeout, no countdown.
    If _marvin_guesser_task fails or errors out, the game freezes forever.
    """
    from cogs.busted99_cog import Busted99Cog
    from game.busted99.session import Busted99State

    bot = _make_bot()
    cog = _make_cog(bot)
    session, engine, jack_id = await _bootstrap_guessing(cog, human_is_guesser=False)

    cog._post_game_message = AsyncMock()
    cog._play_sfx = AsyncMock()
    # Stub marvin guesser so it doesn't actually call engine
    cog._marvin_guesser_task = MagicMock(return_value=_noop_coro())

    spawned_coro_names: list[str] = []
    original_spawn = cog._spawn

    def _recording_spawn(coro):
        spawned_coro_names.append(type(coro).__name__ if hasattr(coro, '__name__') else coro.__class__.__name__)
        return original_spawn(coro)

    cog._spawn = _recording_spawn

    # Trigger on_state_change with Marvin as current guesser
    await cog.on_state_change(session)

    assert cog._guesser_timeout_task is not None, (
        "Bug D: _guesser_timeout_task must be spawned even when Marvin is guessing. "
        "If _marvin_guesser_task fails, the game has no fallback — it freezes."
    )


@pytest.mark.asyncio
async def test_bugD_marvin_guesser_countdown_loop_spawned():
    """
    _guessing_countdown_loop must be spawned even when Marvin is the guesser,
    so the embed timer visually counts down (not frozen at 15s).
    """
    from cogs.busted99_cog import Busted99Cog

    bot = _make_bot()
    cog = _make_cog(bot)
    session, engine, jack_id = await _bootstrap_guessing(cog, human_is_guesser=False)

    cog._post_game_message = AsyncMock()
    cog._play_sfx = AsyncMock()
    cog._marvin_guesser_task = MagicMock(return_value=_noop_coro())

    tasks_spawned: list = []
    original_spawn = cog._spawn

    def _recording_spawn(coro):
        tasks_spawned.append(coro)
        return original_spawn(coro)

    cog._spawn = _recording_spawn

    await cog.on_state_change(session)

    coro_class_names = [c.__class__.__name__ for c in tasks_spawned]
    # coroutine objects have __qualname__
    coro_qual_names = [getattr(c, "__qualname__", "") for c in tasks_spawned]
    all_names = " ".join(coro_class_names + coro_qual_names)

    assert "countdown" in all_names.lower() or any(
        "countdown" in getattr(t, "__qualname__", "").lower()
        for t in tasks_spawned
    ), (
        "Bug D: _guessing_countdown_loop coroutine must be spawned for Marvin's "
        "guesser turns so the timer updates visually (not frozen at 15s)."
    )


# ══════════════════════════════════════════════════════════════════════════════
# Bug B — follow-up wake window 沒有 game_mode 防護
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_bugB_followup_window_not_triggered_in_game_mode():
    """
    When game_mode=True, the follow-up wake window must NOT override is_fast.
    An open follow-up window from before game start must be ignored during game.

    Before fix: handle_stt_result line 1715 has no game_mode guard.
    If _fusion.is_open() is True (window from before game), is_fast is forced
    True → number goes to query_queue (LLM), process_debounced_speech is never
    called → game routing never fires → "喊出數字沒有反應".
    """
    import sys
    import types

    # Build a minimal stub for wake_fusion that returns is_open() = True
    class _FakeFusion:
        def is_open(self):
            return True

        def multi_channel_decide(self, **kwargs):
            # Returns (is_fast=False, confidence, channels)
            return False, 0.0, {
                "voice": 0.0, "task": 0.0, "info": 0.0,
                "control": 0.0, "total": 0.0, "threshold": 0.5,
            }

    # We test the guard directly by reading the code path condition.
    # The fix: `if not is_fast and not self.game_mode and not self._wake_response_pending
    #             and _fusion is not None and _fusion.is_open():`
    # Before fix: no `not self.game_mode` check.

    # We simulate the decision logic in isolation.
    game_mode = True
    is_fast = False
    wake_response_pending = False
    fusion = _FakeFusion()

    # CURRENT (buggy) condition — no game_mode guard:
    buggy_would_override = (
        not is_fast
        and not wake_response_pending
        and fusion is not None
        and fusion.is_open()
    )

    # FIXED condition — with game_mode guard:
    fixed_would_override = (
        not is_fast
        and not game_mode          # ← this guard is the fix
        and not wake_response_pending
        and fusion is not None
        and fusion.is_open()
    )

    assert buggy_would_override is True, (
        "test setup error: buggy condition should trigger override"
    )
    assert fixed_would_override is False, (
        "Bug B: fixed condition must NOT override is_fast when game_mode=True, "
        "even if the follow-up window is open."
    )

    # Verify the fix is in place. 喚醒守衛叢集已抽到 _apply_wake_guards（行為不變），
    # follow-up override guard 現在住在那裡，所以檢查該方法的 source。
    import inspect
    from cogs import voice_controller as vc_mod
    src = inspect.getsource(vc_mod.VoiceController._apply_wake_guards)
    # The guard line should contain both "game_mode" and "is_open"
    guard_lines = [
        line.strip() for line in src.splitlines()
        if "is_open" in line and "is_fast" in line
    ]
    assert any("game_mode" in l for l in guard_lines), (
        "Bug B: handle_stt_result follow-up window guard (line with is_open + is_fast) "
        "must include a `game_mode` check so numbers are not routed to LLM during a game."
    )


# ── tiny helper ──────────────────────────────────────────────────────────────

async def _noop_coro():
    pass

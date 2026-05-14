"""
TDD 測試：出題輸入時間改為 120 秒 + 第一回合 skip 不崩潰

Bug 1: SetterInputView timeout=35，35 秒後按鈕消失，看起來崩潰。
       修復：改為 120 秒，同時 _setter_timeout_task 也在 120 秒觸發。

Bug 2: skip_setter_timeout 不重置回合狀態（current_round, wrong_guesses 等），
       轉到下一位時遊戲狀態殘留前一回合的資料。
       修復：skip_setter_timeout 做與 next_round 相同的重置。
"""
from __future__ import annotations

import asyncio
import inspect
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from game.session import GameSession, GameState, PlayerState
from game.engine import GameEngine


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_bot():
    bot = MagicMock()
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    return bot


def _make_cog(bot=None):
    if bot is None:
        bot = _make_bot()
    with patch("cogs.game_cog.MemoryManager"):
        from cogs.game_cog import BustedCog
        return BustedCog(bot)


def _engine_with_two_players() -> tuple[GameEngine, GameSession]:
    session = GameSession(session_id="t1", guild_id=1, channel_id=1)

    async def _noop(s):
        pass

    engine = GameEngine(session=session, on_state_change=_noop, db_path=":memory:")
    return engine, session


# ══════════════════════════════════════════════════════════════════════════════
# Bug 1 — 輸入時間改為 120 秒
# ══════════════════════════════════════════════════════════════════════════════

def test_setter_input_view_timeout_is_120():
    """SetterInputView 的 Discord view timeout 必須是 120 秒。"""
    from cogs.game_cog import SetterInputView
    cog = _make_cog()
    view = SetterInputView(cog, "user_a")
    assert view.timeout == 120, (
        f"SetterInputView.timeout 應為 120，目前是 {view.timeout}。"
        "35 秒後按鈕消失會讓使用者以為遊戲崩潰。"
    )


def test_setter_timeout_task_sleeps_120():
    """_setter_timeout_task 應在 120 秒後觸發（而非 150）。"""
    from cogs import game_cog as gc_mod
    src = inspect.getsource(gc_mod.BustedCog._setter_timeout_task)
    assert "120" in src, (
        "_setter_timeout_task 應 sleep(120)，目前仍是 150。"
        "150 秒讓按鈕消失後又靜音 115 秒，看起來像崩潰。"
    )
    assert "150" not in src, (
        "_setter_timeout_task 不應再有 150，請改為 120。"
    )


def test_setter_input_embed_mentions_120s():
    """出題 embed 應顯示 120 秒。"""
    cog = _make_cog()
    session = GameSession(session_id="t1", guild_id=1, channel_id=1)
    session.players = [PlayerState(user_id="u1", display_name="Alice")]
    session.current_setter_id = "u1"
    embed = cog._build_setter_input_embed(session)
    all_text = " ".join(f.value for f in embed.fields) + (embed.description or "")
    assert "120" in all_text, (
        f"出題 embed 應顯示 120 秒，目前文字：{all_text!r}"
    )


def test_timer_for_setter_input_is_120():
    """_timer_for_state(SETTER_INPUT) 應回傳 120。"""
    cog = _make_cog()
    result = cog._timer_for_state(GameState.SETTER_INPUT)
    assert result == 120, (
        f"_timer_for_state(SETTER_INPUT) 應為 120，目前是 {result}。"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Bug 2 — skip_setter_timeout 第一回合崩潰：需重置回合狀態
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_skip_setter_timeout_resets_round_state():
    """
    skip_setter_timeout 跳到下一位時，應重置回合內狀態：
    current_answer, current_clues, current_round, wrong_guesses,
    current_theme, candidate_themes。
    這樣第二位出題人進入 SETTER_INPUT 是乾淨的狀態。
    """
    engine, session = _engine_with_two_players()
    await engine.add_player("user_a", "Alice")
    await engine.add_player("user_b", "Bob")

    # 模擬第一回合已進行一段時間但未完成
    session.current_setter_id = "user_a"
    session.remaining_setters = ["user_b"]
    session.state = GameState.SETTER_INPUT
    session.current_theme = "舊主題"
    session.candidate_themes = ["舊主題", "另一個"]
    session.current_answer = None   # 從未提交
    session.current_clues = []
    session.current_round = 1
    session.wrong_guesses = ["錯誤猜測"]  # 殘留資料

    await engine.skip_setter_timeout()

    assert session.current_theme is None, "current_theme 應被清除"
    assert session.candidate_themes == [], "candidate_themes 應被清除"
    assert session.current_answer is None, "current_answer 應為 None"
    assert session.current_clues == [], "current_clues 應為空"
    assert session.current_round == 1, "current_round 應重置為 1"
    assert session.wrong_guesses == [], "wrong_guesses 應被清除"


@pytest.mark.asyncio
async def test_skip_setter_timeout_first_round_advances_to_spinning():
    """
    第一回合出題人 timeout → state 應變為 SPINNING，
    current_setter_id 應指向下一個玩家。
    """
    engine, session = _engine_with_two_players()
    await engine.add_player("user_a", "Alice")
    await engine.add_player("user_b", "Bob")

    session.current_setter_id = "user_a"
    session.remaining_setters = ["user_b"]
    session.state = GameState.SETTER_INPUT

    await engine.skip_setter_timeout()

    assert session.state == GameState.SPINNING, (
        f"應轉到 SPINNING，目前 state={session.state}"
    )
    assert session.current_setter_id == "user_b", (
        f"current_setter_id 應為 user_b，目前是 {session.current_setter_id}"
    )


@pytest.mark.asyncio
async def test_skip_setter_timeout_no_remaining_setters_ends_game():
    """
    skip_setter_timeout 後若無剩餘出題人，應進入 GAME_OVER。
    """
    engine, session = _engine_with_two_players()
    await engine.add_player("user_a", "Alice")

    session.current_setter_id = "user_a"
    session.remaining_setters = []  # 無人可接
    session.state = GameState.SETTER_INPUT

    await engine.skip_setter_timeout()

    assert session.state == GameState.GAME_OVER, (
        f"無剩餘出題人應進入 GAME_OVER，目前 state={session.state}"
    )


@pytest.mark.asyncio
async def test_skip_setter_timeout_penalises_setter():
    """skip_setter_timeout 應對逾時出題人扣分。"""
    from game.engine import SETTER_TIMEOUT_PENALTY
    engine, session = _engine_with_two_players()
    await engine.add_player("user_a", "Alice")
    await engine.add_player("user_b", "Bob")

    session.current_setter_id = "user_a"
    session.remaining_setters = ["user_b"]
    session.state = GameState.SETTER_INPUT

    alice = next(p for p in session.players if p.user_id == "user_a")
    alice.score = 100

    await engine.skip_setter_timeout()

    assert alice.score == 100 + SETTER_TIMEOUT_PENALTY, (
        f"Alice 應被扣 {abs(SETTER_TIMEOUT_PENALTY)} 分，目前 score={alice.score}"
    )

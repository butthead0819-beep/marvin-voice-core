"""
TDD tests for Busted99 engine.

Run with:
    pytest tests/test_busted99_engine.py -v
"""
from __future__ import annotations

import pytest

from game.busted99.session import Busted99Session, Busted99State, Player99State
from game.busted99.scoring import score_for_space


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_session(session_id: str = "test-session") -> Busted99Session:
    return Busted99Session(
        session_id=session_id,
        guild_id=1,
        channel_id=1,
    )


async def _noop_state_change(session):
    pass


def _make_engine(session: Busted99Session | None = None, db_path: str = ":memory:"):
    from game.busted99.engine import Busted99Engine
    if session is None:
        session = _make_session()
    return Busted99Engine(
        session=session,
        on_state_change=_noop_state_change,
        db_path=db_path,
    )


async def _setup_guessing(engine, setter_id: str = "p1", answer: int = 42):
    """Helper: add 3 players, start game, set answer, return engine in GUESSING state."""
    engine.session.players = [
        Player99State(user_id="p1", display_name="Alice"),
        Player99State(user_id="p2", display_name="Bob"),
        Player99State(user_id="p3", display_name="Carol"),
    ]
    engine.session.state = Busted99State.JOINING
    await engine.start_game()
    # Force setter to p1 for deterministic tests
    engine.session.setter_id = setter_id
    engine.session.state = Busted99State.SETTER_PICKING
    await engine.set_answer(setter_id, answer)
    return engine


# ── 1. add_player ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_player_returns_true_when_valid():
    engine = _make_engine()
    result = await engine.add_player("u1", "Alice")
    assert result is True
    assert engine.session.state == Busted99State.JOINING
    assert any(p.user_id == "u1" for p in engine.session.players)


@pytest.mark.asyncio
async def test_add_player_rejects_duplicate():
    engine = _make_engine()
    await engine.add_player("u1", "Alice")
    result = await engine.add_player("u1", "Alice")
    assert result is False
    assert len([p for p in engine.session.players if p.user_id == "u1"]) == 1


@pytest.mark.asyncio
async def test_add_player_rejects_when_game_active():
    engine = _make_engine()
    await engine.add_player("u1", "Alice")
    await engine.add_player("u2", "Bob")
    engine.session.state = Busted99State.GUESSING  # simulate active game
    result = await engine.add_player("u3", "Carol")
    assert result is False


# ── 2. start_game ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_game_picks_setter_from_players():
    engine = _make_engine()
    await engine.add_player("u1", "Alice")
    await engine.add_player("u2", "Bob")
    await engine.start_game()
    assert engine.session.state == Busted99State.SETTER_PICKING
    assert engine.session.setter_id in ("u1", "u2")
    assert engine.session.started_at > 0


# ── 3. set_answer ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_answer_rejects_out_of_range():
    engine = _make_engine()
    await engine.add_player("u1", "Alice")
    await engine.add_player("u2", "Bob")
    await engine.start_game()
    engine.session.setter_id = "u1"
    engine.session.state = Busted99State.SETTER_PICKING
    result_zero = await engine.set_answer("u1", 0)
    assert result_zero is False
    result_hundred = await engine.set_answer("u1", 100)
    assert result_hundred is False


@pytest.mark.asyncio
async def test_set_answer_transitions_to_guessing():
    engine = _make_engine()
    await engine.add_player("u1", "Alice")
    await engine.add_player("u2", "Bob")
    await engine.start_game()
    setter = engine.session.setter_id
    result = await engine.set_answer(setter, 50)
    assert result is True
    assert engine.session.state == Busted99State.GUESSING
    assert engine.session.answer == 50
    assert engine.session.current_guesser_id is not None


@pytest.mark.asyncio
async def test_set_answer_rejects_wrong_setter():
    engine = _make_engine()
    await engine.add_player("u1", "Alice")
    await engine.add_player("u2", "Bob")
    await engine.start_game()
    # Find who is NOT setter
    setter = engine.session.setter_id
    non_setter = "u2" if setter == "u1" else "u1"
    result = await engine.set_answer(non_setter, 50)
    assert result is False


# ── 4. submit_guess — correct ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_guess_correct_busts_guesser():
    engine = _make_engine()
    await _setup_guessing(engine, setter_id="p1", answer=42)
    guesser_id = engine.session.current_guesser_id
    result = await engine.submit_guess(guesser_id, 42)
    assert result["result"] == "bust"
    # Guesser should have 0 score (busted)
    guesser = next(p for p in engine.session.players if p.user_id == guesser_id)
    assert guesser.score == 0
    assert engine.session.state == Busted99State.GAME_OVER


@pytest.mark.asyncio
async def test_submit_guess_correct_others_score():
    engine = _make_engine()
    await _setup_guessing(engine, setter_id="p1", answer=42)
    guesser_id = engine.session.current_guesser_id
    result = await engine.submit_guess(guesser_id, 42)
    # Other players (non-guesser) should have positive score
    others = [p for p in engine.session.players if p.user_id != guesser_id]
    for p in others:
        assert p.score > 0, f"Player {p.user_id} should have score > 0 after bust"


# ── 5. submit_guess — wrong ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_guess_wrong_low_narrows_range():
    engine = _make_engine()
    await _setup_guessing(engine, setter_id="p1", answer=60)
    guesser_id = engine.session.current_guesser_id
    result = await engine.submit_guess(guesser_id, 40)
    assert result["result"] == "wrong_low"
    assert engine.session.low_bound == 40   # 猜過的數字本身成為新下界
    assert engine.session.high_bound == 99  # unchanged


@pytest.mark.asyncio
async def test_submit_guess_wrong_high_narrows_range():
    engine = _make_engine()
    await _setup_guessing(engine, setter_id="p1", answer=30)
    guesser_id = engine.session.current_guesser_id
    result = await engine.submit_guess(guesser_id, 50)
    assert result["result"] == "wrong_high"
    assert engine.session.high_bound == 50  # 猜過的數字本身成為新上界
    assert engine.session.low_bound == 1    # unchanged


@pytest.mark.asyncio
async def test_submit_guess_out_of_range_rejected():
    engine = _make_engine()
    await _setup_guessing(engine, setter_id="p1", answer=50)
    # Narrow range first
    engine.session.low_bound = 30
    engine.session.high_bound = 70
    guesser_id = engine.session.current_guesser_id
    # Out of range low
    result_low = await engine.submit_guess(guesser_id, 20)
    assert result_low["result"] == "out_of_range"
    # Out of range high
    result_high = await engine.submit_guess(guesser_id, 80)
    assert result_high["result"] == "out_of_range"


# ── 6. timeout ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_timeout_deducts_score():
    engine = _make_engine()
    await _setup_guessing(engine, setter_id="p1", answer=50)
    # 先給 guesser 一些分數，這樣扣分後才有意義
    guesser_id = engine.session.current_guesser_id
    guesser = next(p for p in engine.session.players if p.user_id == guesser_id)
    guesser.score = 50  # 給 50 分後扣分
    space = engine.session.high_bound - engine.session.low_bound + 1
    expected_deduction = score_for_space(space)
    result = await engine.timeout_guesser()
    guesser = next(p for p in engine.session.players if p.user_id == guesser_id)
    assert guesser.score == max(0, 50 - expected_deduction)
    assert result["deducted"] == expected_deduction


@pytest.mark.asyncio
async def test_timeout_score_floor_is_zero():
    """超時扣分不會讓玩家分數低於 0（D3 規格）。"""
    engine = _make_engine()
    await _setup_guessing(engine, setter_id="p1", answer=50)
    guesser_id = engine.session.current_guesser_id
    guesser = next(p for p in engine.session.players if p.user_id == guesser_id)
    guesser.score = 0  # 分數為 0，扣分後不能變負數
    result = await engine.timeout_guesser()
    guesser = next(p for p in engine.session.players if p.user_id == guesser_id)
    assert guesser.score == 0
    assert result["deducted"] > 0


@pytest.mark.asyncio
async def test_timeout_advances_to_next_guesser():
    engine = _make_engine()
    await _setup_guessing(engine, setter_id="p1", answer=50)
    first_guesser = engine.session.current_guesser_id
    result = await engine.timeout_guesser()
    # Next guesser should be different or None (if only one non-setter)
    # With 3 players (p1=setter, p2, p3), should advance
    assert engine.session.current_guesser_id != first_guesser or result["next_guesser_id"] is not None


# ── 7. round advancement ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_all_guessers_used_starts_new_round():
    engine = _make_engine()
    engine.session.players = [
        Player99State(user_id="p1", display_name="Alice"),
        Player99State(user_id="p2", display_name="Bob"),
        Player99State(user_id="p3", display_name="Carol"),
    ]
    engine.session.state = Busted99State.JOINING
    await engine.start_game()
    engine.session.setter_id = "p1"
    engine.session.state = Busted99State.SETTER_PICKING
    await engine.set_answer("p1", 50)

    initial_round = engine.session.round_num
    # Use up all guessers with wrong guesses
    # p1 is setter, so p2 and p3 are guessers
    for _ in range(10):  # Exhaust all rounds
        if engine.session.state != Busted99State.GUESSING:
            break
        guesser_id = engine.session.current_guesser_id
        # Narrow the answer slightly so it stays wrong (避開邊界值，符合新規則)
        if engine.session.low_bound < 50:
            await engine.submit_guess(guesser_id, engine.session.low_bound + 1)
        elif engine.session.high_bound > 50:
            await engine.submit_guess(guesser_id, engine.session.high_bound - 1)
        else:
            break  # Space is 1, next guess would be correct or last_chance

    # After all original guessers used, round_num should have increased
    assert engine.session.round_num > initial_round or engine.session.state == Busted99State.GAME_OVER


# ── 8. last chance (space ≤ 2) ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_last_chance_correct_setter_gets_100():
    engine = _make_engine()
    engine.session.players = [
        Player99State(user_id="p1", display_name="Alice"),
        Player99State(user_id="p2", display_name="Bob"),
        Player99State(user_id="p3", display_name="Carol"),
    ]
    engine.session.state = Busted99State.JOINING
    await engine.start_game()
    engine.session.setter_id = "p1"
    engine.session.state = Busted99State.SETTER_PICKING
    await engine.set_answer("p1", 50)

    # Force space ≤ 2
    engine.session.low_bound = 49
    engine.session.high_bound = 50
    engine.session.state = Busted99State.GUESSING
    # Ensure a non-setter is guessing
    non_setters = [p.user_id for p in engine.session.players if p.user_id != "p1"]
    engine.session.current_guesser_id = non_setters[0]

    result = await engine.submit_guess(non_setters[0], 50)
    assert result["result"] == "last_bust"
    setter = next(p for p in engine.session.players if p.user_id == "p1")
    assert setter.score == 100
    assert engine.session.state == Busted99State.GAME_OVER


@pytest.mark.asyncio
async def test_last_chance_wrong_guesser_gets_100():
    engine = _make_engine()
    engine.session.players = [
        Player99State(user_id="p1", display_name="Alice"),
        Player99State(user_id="p2", display_name="Bob"),
        Player99State(user_id="p3", display_name="Carol"),
    ]
    engine.session.state = Busted99State.JOINING
    await engine.start_game()
    engine.session.setter_id = "p1"
    engine.session.state = Busted99State.SETTER_PICKING
    await engine.set_answer("p1", 50)

    # Force space ≤ 2
    engine.session.low_bound = 49
    engine.session.high_bound = 50
    engine.session.state = Busted99State.GUESSING
    non_setters = [p.user_id for p in engine.session.players if p.user_id != "p1"]
    engine.session.current_guesser_id = non_setters[0]

    result = await engine.submit_guess(non_setters[0], 49)  # Wrong guess (answer is 50)
    assert result["result"] == "last_wrong"
    guesser = next(p for p in engine.session.players if p.user_id == non_setters[0])
    assert guesser.score == 100
    assert engine.session.state == Busted99State.GAME_OVER


# ── 9. parse_number ───────────────────────────────────────────────────────────

def test_parse_number_arabic():
    from game.busted99.engine import parse_number
    assert parse_number("42") == 42
    assert parse_number("99") == 99
    assert parse_number("1") == 1
    assert parse_number("我猜 57") == 57
    assert parse_number("  15  ") == 15


def test_parse_number_chinese():
    from game.busted99.engine import parse_number
    assert parse_number("四十二") == 42
    assert parse_number("四十") == 40
    assert parse_number("三十五") == 35
    assert parse_number("十二") == 12
    assert parse_number("七") == 7
    assert parse_number("一") == 1
    assert parse_number("九十九") == 99
    assert parse_number("二十") == 20


def test_parse_number_invalid_returns_none():
    from game.busted99.engine import parse_number
    assert parse_number("我不知道") is None
    assert parse_number("") is None
    assert parse_number("一百") is None   # 100 out of range
    assert parse_number("零") is None     # 0 out of range


# ── 10. score_for_space ───────────────────────────────────────────────────────

def test_score_for_space_min_is_10():
    # space 90-99 should give 10
    assert score_for_space(99) == 10
    assert score_for_space(90) == 10


def test_score_for_space_max_is_100():
    # space 1-9 should give 100
    assert score_for_space(1) == 100
    assert score_for_space(9) == 100


def test_score_for_space_buckets():
    assert score_for_space(99) == 10
    assert score_for_space(90) == 10
    assert score_for_space(89) == 20
    assert score_for_space(80) == 20
    assert score_for_space(79) == 30
    assert score_for_space(70) == 30
    assert score_for_space(69) == 40
    assert score_for_space(60) == 40
    assert score_for_space(59) == 50
    assert score_for_space(50) == 50
    assert score_for_space(49) == 60
    assert score_for_space(40) == 60
    assert score_for_space(39) == 70
    assert score_for_space(30) == 70
    assert score_for_space(29) == 80
    assert score_for_space(20) == 80
    assert score_for_space(19) == 90
    assert score_for_space(10) == 90
    assert score_for_space(9) == 100
    assert score_for_space(1) == 100


# ── 11. _save_guess arity ─────────────────────────────────────────────────────

import inspect as _inspect
from game.busted99.engine import Busted99Engine as _Busted99Engine


def test_save_guess_signature_matches_call_sites():
    """_save_guess must accept 10 params (excluding self): 9 required + all_scores_json optional.
    Call sites pass 9 (base engine, under lock, session safe) or 10 (llm_engine, pre-snapshot).
    A mismatch causes silent DB write failures since run_in_executor discards exceptions."""
    sig = _inspect.signature(_Busted99Engine._save_guess)
    params = [p for p in sig.parameters if p != "self"]
    assert len(params) == 10, (
        f"_save_guess has {len(params)} params (excluding self), expected 10 (9 required + all_scores_json). "
        "Update call sites if you add/remove params."
    )
    # all_scores_json must be optional (has a default)
    last_param = sig.parameters["all_scores_json"]
    assert last_param.default is not _inspect.Parameter.empty


# ── 12. timeout_guesser returns timed_out metadata ────────────────────────────

@pytest.mark.asyncio
async def test_timeout_result_includes_timed_out_guesser_id():
    """timeout_guesser must return timed_out_guesser_id so cog can show the right name."""
    engine = _make_engine()
    await _setup_guessing(engine, setter_id="p1", answer=50)
    guesser_before = engine.session.current_guesser_id
    result = await engine.timeout_guesser()
    assert "timed_out_guesser_id" in result
    assert result["timed_out_guesser_id"] == guesser_before
    assert "timed_out_name" in result
    assert result["timed_out_name"] != ""


# ── 13. S2: set_answer with no non-setters ends game ─────────────────────────

@pytest.mark.asyncio
async def test_set_answer_with_no_non_setters_ends_game():
    """
    S2 修正：當只有 Marvin（setter）一人時，non_setters 為空，
    set_answer 應直接將 state 設為 GAME_OVER，而非進入 GUESSING
    （避免無限 timeout loop）。
    """
    engine = _make_engine()
    # 只加入 Marvin 一人
    ok = await engine.add_player("marvin", "Marvin")
    assert ok is True
    # 手動推入 SETTER_PICKING
    engine.session.setter_id = "marvin"
    engine.session.state = Busted99State.SETTER_PICKING
    result = await engine.set_answer("marvin", 42)
    assert result is True
    # 應直接結束遊戲，不能卡在 GUESSING
    assert engine.session.state == Busted99State.GAME_OVER


# ── 終極密碼：禁猜邊界 ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_guess_boundary_rejected_when_space_gt2():
    """space > 2 時，猜 low_bound 或 high_bound 應回傳 boundary，不消耗回合。"""
    engine = await _setup_guessing(_make_engine(), answer=42)
    # 縮小範圍到 30-50（space=21），猜邊界 30
    engine.session.low_bound = 30
    engine.session.high_bound = 50
    guesser_id = engine.session.current_guesser_id
    result = await engine.submit_guess(guesser_id, 30)
    assert result["result"] == "boundary"
    # 回合不消耗：仍是同一個 guesser
    assert engine.session.current_guesser_id == guesser_id
    assert engine.session.state == Busted99State.GUESSING


@pytest.mark.asyncio
async def test_submit_guess_high_boundary_rejected_when_space_gt2():
    """space > 2 時，猜 high_bound 也應拒絕。"""
    engine = await _setup_guessing(_make_engine(), answer=42)
    engine.session.low_bound = 30
    engine.session.high_bound = 50
    guesser_id = engine.session.current_guesser_id
    result = await engine.submit_guess(guesser_id, 50)
    assert result["result"] == "boundary"
    assert engine.session.current_guesser_id == guesser_id


@pytest.mark.asyncio
async def test_submit_guess_boundary_allowed_at_space2():
    """終極密碼（space == 2）時，猜邊界是合法的。"""
    engine = await _setup_guessing(_make_engine(), answer=45)
    engine.session.low_bound = 44
    engine.session.high_bound = 45
    guesser_id = engine.session.current_guesser_id
    # 猜 low_bound（44）不是答案，應是 wrong_low（答案在高側）
    # 但不應被拒絕為 boundary
    result = await engine.submit_guess(guesser_id, 44)
    assert result["result"] != "boundary"


@pytest.mark.asyncio
async def test_submit_guess_non_boundary_accepted_when_space_gt2():
    """space > 2 時，猜非邊界數字正常處理（不被 boundary 攔截）。"""
    engine = await _setup_guessing(_make_engine(), answer=42)
    engine.session.low_bound = 30
    engine.session.high_bound = 50
    guesser_id = engine.session.current_guesser_id
    result = await engine.submit_guess(guesser_id, 40)  # 非邊界
    assert result["result"] in ("wrong_low", "wrong_high", "bust", "last_bust")


@pytest.mark.asyncio
async def test_submit_guess_boundary_space_eq3_rejected():
    """space == 3 時仍屬 space > 2，邊界應被拒絕。"""
    engine = await _setup_guessing(_make_engine(), answer=32)
    engine.session.low_bound = 31
    engine.session.high_bound = 33
    guesser_id = engine.session.current_guesser_id
    result = await engine.submit_guess(guesser_id, 31)
    assert result["result"] == "boundary"


# ── guesser_order 固定順序 ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_guesser_order_fixed_after_set_answer():
    """set_answer 後 guesser_order 建立且不含 setter。"""
    engine = await _setup_guessing(_make_engine(), answer=50)
    assert "p1" not in engine.session.guesser_order
    assert set(engine.session.guesser_order) == {"p2", "p3"}


@pytest.mark.asyncio
async def test_guesser_order_same_across_rounds():
    """新輪開始時猜題順序應與第一輪相同（不重新 shuffle）。"""
    engine = await _setup_guessing(_make_engine(), answer=50)
    first_round_order = list(engine.session.guesser_order)

    # 消耗完第一輪所有 guesser（p1 是 setter，所以兩個非 setter 各猜一次）
    for _ in range(len(first_round_order)):
        if engine.session.state != Busted99State.GUESSING:
            break
        gid = engine.session.current_guesser_id
        # 猜邊界外的數字讓遊戲繼續（low_bound+1 且不等於 50）
        guess = engine.session.low_bound + 1
        if guess == 50:
            guess = engine.session.high_bound - 1
        await engine.submit_guess(gid, guess)

    # 現在應該進入第二輪
    assert engine.session.round_num == 2
    # 第二輪第一個猜題者應與第一輪第一個相同
    assert engine.session.current_guesser_id == first_round_order[0]
    # queue 也應與第一輪剩餘一致
    assert engine.session.guessing_queue == first_round_order[1:]

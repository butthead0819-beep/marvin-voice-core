"""Full-game scenario test for Busted.

Walks the engine from JOINING through GAME_OVER using the shared simulator
core (also exercised by scripts/busted_demo.py for visual playback).

Run with `pytest -sv tests/test_busted_full_game.py` to see the state
trail printed inline.
"""
from __future__ import annotations

import pytest

from game.session import GameState
from scripts.busted_sim_core import (
    BuzzAttempt,
    GameRecorder,
    RoundPlan,
    build_engine,
    play_full_game,
)


PLAYERS = [
    ("u_alice", "Alice"),
    ("u_bob",   "Bob"),
    ("u_carol", "Carol"),
    ("u_dave",  "Dave"),
]

CANNED_CLUES = {
    "巨石強森": ["他是好萊塢動作明星", "他從 WWE 出身", "綽號 The Rock", "玩命關頭主演", "Dwayne Johnson"],
    "周杰倫":   ["台灣天王歌手", "外號周董", "雙截棍稻香", "妻子昆凌", "Jay Chou"],
    "黑洞":     ["最神秘天體", "光也逃不出", "恆星塌縮", "事件視界", "兩個字"],
    "拉麵":     ["日本湯麵", "豚骨味噌", "叉燒蔥花", "札幌博多", "兩個字"],
}

# Score model (see game/scoring.py):
#   clue_round 1 → guesser 100 / setter 20
#   clue_round 2 → guesser 80  / setter 40
#   clue_round 3 → guesser 60  / setter 60
#   clue_round 4 → guesser 40  / setter 80
#   round 5 final → setter 100 if any partial / -100 penalty otherwise
#
# Expected outcome:
#   R1 Alice setter, Bob buzz@clue2 wrong, Carol buzz@clue3 correct → Carol +60, Alice +60
#   R2 Bob setter,   Carol buzz@clue1 correct                       → Carol +100, Bob +20
#   R3 Carol setter, Alice buzz@clue2 wrong, Dave buzz@clue4 correct→ Dave +40, Carol +80
#   R4 Dave setter,  nobody buzzes → walks past round 5 → Dave -100 penalty
PLAN = [
    RoundPlan(setter_id="u_alice", theme="電影", answer="巨石強森", buzzes=[
        BuzzAttempt(after_clue=2, user_id="u_bob",   guess="阿諾"),
        BuzzAttempt(after_clue=3, user_id="u_carol", guess="巨石強森"),
    ]),
    RoundPlan(setter_id="u_bob", theme="音樂", answer="周杰倫", buzzes=[
        BuzzAttempt(after_clue=1, user_id="u_carol", guess="周杰倫"),
    ]),
    RoundPlan(setter_id="u_carol", theme="天文", answer="黑洞", buzzes=[
        BuzzAttempt(after_clue=2, user_id="u_alice", guess="星星"),
        BuzzAttempt(after_clue=4, user_id="u_dave",  guess="黑洞"),
    ]),
    RoundPlan(setter_id="u_dave", theme="美食", answer="拉麵", buzzes=[]),
]

EXPECTED_FINAL_SCORES = {
    "Alice": 60,                # R1 setter (Carol correct @ clue 3 → 60)
    "Bob":   20,                # R2 setter (Carol correct @ clue 1 → 20)
    "Carol": 60 + 100 + 80,     # R1 guess + R2 guess + R3 setter (Dave correct @ clue 4 → 80)
    "Dave":  40 - 100,          # R3 guess + R4 no-buzz setter penalty
}


@pytest.mark.asyncio
async def test_full_game_4_rounds_code_judge():
    """Drive a full 4-player game with code-judge engine. Verifies state
    sequence, score arithmetic, and that GAME_OVER fires at the end."""
    recorder = GameRecorder()
    engine = build_engine(use_llm=False, canned_clues=CANNED_CLUES, recorder=recorder)
    for uid, name in PLAYERS:
        ok = await engine.add_player(uid, name)
        assert ok, f"add_player({uid}) failed"

    results = await play_full_game(engine, PLAN)

    # ── 4 rounds resolved ────────────────────────────────────────────────
    assert len(results) == 4, f"expected 4 round results, got {len(results)}"

    # ── Final state is GAME_OVER ──────────────────────────────────────────
    assert engine.session.state == GameState.GAME_OVER, (
        f"expected GAME_OVER, got {engine.session.state}"
    )

    # ── Score arithmetic matches the scoring rules ───────────────────────
    actual_scores = {p.display_name: p.score for p in engine.session.players}
    assert actual_scores == EXPECTED_FINAL_SCORES, (
        f"score mismatch\n  expected: {EXPECTED_FINAL_SCORES}\n  actual:   {actual_scores}"
    )

    # ── State trail covers every phase at least once ──────────────────────
    states_seen = {t.state for t in recorder.transitions}
    required = {
        GameState.JOINING, GameState.SPINNING, GameState.THEME_SELECT,
        GameState.SETTER_INPUT, GameState.CLUE_ACTIVE, GameState.BUZZ_LOCKED,
        GameState.ROUND_RESULT, GameState.GAME_OVER,
    }
    missing = required - states_seen
    assert not missing, f"states never visited: {missing}"

    # ── round_num advances 1→4 ────────────────────────────────────────────
    round_nums = sorted({t.round_num for t in recorder.transitions if t.state == GameState.ROUND_RESULT})
    assert round_nums == [1, 2, 3, 4], f"round progression {round_nums}"

    # ── action_log captured both correct + wrong events ──────────────────
    log = engine.session.action_log
    types = [e["type"] for e in log]
    assert "buzz" in types
    assert "correct" in types
    assert "wrong" in types
    correct_entries = [e for e in log if e["type"] == "correct"]
    assert len(correct_entries) == 3, (
        f"expected 3 correct entries (R1/R2/R3), got {len(correct_entries)}"
    )


@pytest.mark.asyncio
async def test_full_game_round4_setter_penalty_when_no_buzz():
    """When nobody buzzes for a whole round, the setter takes a -100 penalty
    (round 5 partial-score path with no partial scores)."""
    recorder = GameRecorder()
    engine = build_engine(use_llm=False, canned_clues=CANNED_CLUES, recorder=recorder)
    for uid, name in PLAYERS:
        await engine.add_player(uid, name)

    await play_full_game(engine, PLAN)
    dave = next(p for p in engine.session.players if p.display_name == "Dave")
    # Dave got +40 from R3 guess, then -100 setter penalty in R4 → net -60
    assert dave.score == -60, f"Dave should net -60, got {dave.score}"


@pytest.mark.asyncio
async def test_full_game_records_every_transition():
    """Recorder captures every state transition. Used as the data feed for
    the CLI demo, so this guards the contract."""
    recorder = GameRecorder()
    engine = build_engine(use_llm=False, canned_clues=CANNED_CLUES, recorder=recorder)
    for uid, name in PLAYERS:
        await engine.add_player(uid, name)
    await play_full_game(engine, PLAN)

    # Every transition has the snapshot fields the printer reads.
    for t in recorder.transitions:
        assert hasattr(t, "state")
        assert hasattr(t, "scores")
        assert hasattr(t, "action_log_tail")
        assert isinstance(t.scores, dict)
        assert isinstance(t.action_log_tail, list)

"""Shared simulator core for Busted. Used by:
  - scripts/busted_demo.py (CLI visual playback)
  - tests/test_busted_full_game.py (regression assertions)

Drives a GameEngine (or GameLLMEngine) through a full multi-round game using
canned clues + a scripted "who buzzes with what" plan. Records every state
transition so callers can pretty-print or assert.
"""
from __future__ import annotations

import asyncio
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from game.engine import GameEngine
from game.session import GameSession, GameState


# ── Scenario primitives ──────────────────────────────────────────────────────

@dataclass
class BuzzAttempt:
    """One scripted buzz attempt within a round."""
    after_clue: int        # buzz after the Nth clue is visible (1..5)
    user_id: str
    guess: str


@dataclass
class RoundPlan:
    """One full round: who's the setter, what's the answer, who buzzes."""
    setter_id: str
    theme: str
    answer: str
    buzzes: list[BuzzAttempt] = field(default_factory=list)


# ── Recorder ─────────────────────────────────────────────────────────────────

@dataclass
class Transition:
    state: GameState
    round_num: int
    current_round: int
    setter_id: str | None
    theme: str | None
    answer_len: int
    buzz_holder: str | None
    scores: dict[str, int]
    clues: list[str]
    wrong_guesses: list[str]
    action_log_tail: list[dict]
    last_result: dict[str, Any] | None = None


class GameRecorder:
    """Records every state_change. Optionally invokes a hook for live printing."""

    def __init__(self, on_transition: Callable[[Transition], None] | None = None):
        self.transitions: list[Transition] = []
        self._on_transition = on_transition

    async def __call__(self, session: GameSession) -> None:
        t = Transition(
            state=session.state,
            round_num=session.round_num,
            current_round=session.current_round,
            setter_id=session.current_setter_id,
            theme=session.current_theme,
            answer_len=len(session.current_answer or ""),
            buzz_holder=session.buzz_holder_id,
            scores={p.display_name: p.score for p in session.players},
            clues=list(session.current_clues),
            wrong_guesses=list(session.wrong_guesses),
            action_log_tail=list(getattr(session, "action_log", []))[-5:],
        )
        self.transitions.append(t)
        if self._on_transition:
            self._on_transition(t)


# ── Engine builder ───────────────────────────────────────────────────────────

def build_clue_fn(canned_clues: dict[str, list[str]]) -> Callable[[GameSession], Awaitable[None]]:
    """Return a clue_fn that pops the next canned clue for the current answer
    and re-notifies state_change. Mirrors what cogs/game_cog.py _on_clue_request
    does but without LLM."""
    async def _fn(session: GameSession) -> None:
        answer = session.current_answer or ""
        bucket = canned_clues.get(answer, [])
        idx = len(session.current_clues)
        if idx < len(bucket):
            session.current_clues.append(bucket[idx])
        else:
            session.current_clues.append(f"（線索 {idx + 1}：自動生成）")
    return _fn


def _ephemeral_db_path() -> str:
    """Engine opens a new sqlite3 connection per write, so ':memory:' loses
    the schema between calls. Use a per-process temp file instead."""
    f = tempfile.NamedTemporaryFile(prefix="busted_sim_", suffix=".db", delete=False)
    f.close()
    return str(Path(f.name))


def build_engine(
    *,
    use_llm: bool = False,
    canned_clues: dict[str, list[str]] | None = None,
    recorder: GameRecorder | None = None,
    db_path: str | None = None,
    llm_client: Any = None,
) -> GameEngine:
    if db_path is None:
        db_path = _ephemeral_db_path()
    session = GameSession(
        session_id=str(uuid.uuid4()),
        guild_id=0,
        channel_id=0,
    )
    rec = recorder or GameRecorder()
    clue_fn = build_clue_fn(canned_clues or {})
    if use_llm:
        from game.busted_llm_engine import GameLLMEngine
        engine: GameEngine = GameLLMEngine(
            session=session,
            on_state_change=rec,
            db_path=db_path,
            clue_fn=clue_fn,
            llm_client=llm_client,
        )
    else:
        engine = GameEngine(
            session=session,
            on_state_change=rec,
            db_path=db_path,
            clue_fn=clue_fn,
        )
    # GameEngine.__init__ schedules _init_db via run_in_executor, which races
    # the first _write_round in-flight. Force the schema synchronously so the
    # subsequent fire-and-forget writes always find the tables.
    engine._init_db()
    return engine


# ── Driver ───────────────────────────────────────────────────────────────────

async def _flush_clue_task() -> None:
    """set_answer + advance_clue dispatch clue_fn via create_task — wait for it."""
    # Two yields: one to let create_task schedule, one for the task body to run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def play_round(engine: GameEngine, plan: RoundPlan, themes_pool: list[str]) -> dict:
    """Drive one round from SPINNING through ROUND_RESULT.

    The engine must already be in SPINNING with current_setter_id == plan.setter_id
    (start_game / next_round both leave it there). Returns the final round result.
    """
    s = engine.session
    if s.state != GameState.SPINNING:
        raise RuntimeError(f"play_round expected SPINNING, got {s.state}")
    if s.current_setter_id != plan.setter_id:
        raise RuntimeError(
            f"play_round: spinner picked {s.current_setter_id} but plan expects {plan.setter_id}"
        )

    # SPINNING → THEME_SELECT. Ensure plan.theme is in the candidate slice the
    # cog would show, otherwise select_theme silently returns False.
    candidates = [plan.theme] + [t for t in themes_pool if t != plan.theme]
    await engine.begin_theme_select(candidates[:3])
    await engine.select_theme(plan.theme)

    # SETTER_INPUT → CLUE_ACTIVE + first clue
    await engine.set_answer(plan.answer)
    await _flush_clue_task()

    # Walk buzz plan. Engine auto-advances clues when we call advance_clue.
    result: dict = {"correct": False, "score": 0, "setter_score": 0}
    for buzz in plan.buzzes:
        # Advance clues until we're at the target round
        while engine.session.current_round < buzz.after_clue and engine.session.state == GameState.CLUE_ACTIVE:
            await engine.advance_clue()
            await _flush_clue_task()
        if engine.session.state != GameState.CLUE_ACTIVE:
            break  # round already resolved
        ok = await engine.buzz_in(buzz.user_id)
        if not ok:
            continue
        result = await engine.submit_answer(buzz.user_id, buzz.guess)
        if result.get("correct"):
            break  # round resolved

    # If round didn't resolve, walk clues to round 5 and then advance once more
    # to trigger the round-end transition.
    while engine.session.state == GameState.CLUE_ACTIVE and engine.session.current_round < 5:
        await engine.advance_clue()
        await _flush_clue_task()
    if engine.session.state == GameState.CLUE_ACTIVE and engine.session.current_round >= 5:
        # On round 5, advance_clue triggers the round-end transition (no winner case).
        await engine.advance_clue()

    return result


async def play_full_game(
    engine: GameEngine,
    plans: list[RoundPlan],
    themes_pool: list[str] | None = None,
) -> list[dict]:
    """Run the engine through plans, calling next_round between them.

    All players in plans must already be added via engine.add_player() before
    calling. Returns one result dict per round.
    """
    themes_pool = themes_pool or ["時事", "電影", "音樂", "歷史", "美食"]

    # start_game shuffles remaining_setters then pops the first. Run it, then
    # overwrite the rotation so the deterministic plan order wins.
    await engine.start_game()
    engine.session.current_setter_id = plans[0].setter_id
    engine.session.remaining_setters = [p.setter_id for p in plans[1:]]
    engine.session.state = GameState.SPINNING

    results: list[dict] = []
    for i, plan in enumerate(plans):
        r = await play_round(engine, plan, themes_pool)
        results.append(r)
        # next_round pops remaining_setters[0] for the next round, so the plan
        # order propagates naturally. After the final round it transitions to
        # GAME_OVER.
        if engine.session.state == GameState.ROUND_RESULT:
            await engine.next_round()

    return results

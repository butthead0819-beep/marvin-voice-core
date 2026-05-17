#!/usr/bin/env python3
"""Chaos demo — drive Busted with a fake LLM that randomly emits edge-case text.

Goal: stress-test the defensive code added in the recent fixes:
  - engine.set_answer length gate
  - clue_generator leak post-check + retry
  - MarvinPlayer guess normalization (prefix / punct / quotes / newlines)
  - 3-layer fallback resilience

The fake router and fake LLM client pick from pools of intentionally-bad
strings (leaks, prefixes, oversize, undersize, garbage). Run a full
4-round game with 3 humans + Marvin. If anything explodes the demo dies
with a traceback. If all rounds finish, the defenses held.

Usage:
  python -m scripts.busted_demo_chaos
  python -m scripts.busted_demo_chaos --seed 42       # deterministic
  python -m scripts.busted_demo_chaos --no-color
"""
from __future__ import annotations

import argparse
import asyncio
import random
import sys
from unittest.mock import AsyncMock, MagicMock, patch

from game.session import GameState
from scripts.busted_sim_core import (
    BuzzAttempt,
    GameRecorder,
    RoundPlan,
    build_engine,
    play_full_game,
)
from scripts.busted_demo import C, STATE_LABEL, _fmt_scores, col, _ENABLED

# Import after demo to share the color flag
import scripts.busted_demo as _demo_mod


# ── Edge-case pools ─────────────────────────────────────────────────────────

# Each setter-answer round picks one of these answers. Mix of cleanly bad,
# subtly bad, and fine outputs to exercise different branches of the
# normalizer + length gate.
EDGE_ANSWERS = [
    "巨石強森",            # clean valid
    "周杰倫",              # clean valid (3 chars)
    "黑洞",                # min length valid
    "答案：拉麵",          # prefix-leak + colon
    "「黑洞」",            # bracketed → normalizer strips
    "一",                  # too short → engine rejects → cog fallback to 黑洞
    "我選的是周杰倫",      # narrative wrapper, len > max after strip → truncated to 5
    "超級無敵長的拉麵之神", # too long → truncated
    "",                    # empty → engine rejects
    "黑洞。",              # trailing punct
]

# Clue pool — half leak the answer chars, half clean.
def _gen_edge_clues(answer: str) -> list[str]:
    return [
        # Round 1 — sometimes leaks
        random.choice([
            "一個摔角選手出身的好萊塢動作明星",   # clean
            f"{answer[0]}就是答案的第一個字",      # blatant leak
            "宇宙中神秘且令人沮喪的存在",           # clean
        ]),
        # Round 2 — often leaks
        random.choice([
            "他演過很多動作片",
            f"{answer}就是他",                       # full leak
            "完全與職業摔角無關的某種東西",
        ]),
        # Round 3
        random.choice([
            "曾在 WWE 出現過",
            f"提示：含有「{answer[-1]}」字",         # leak last char
            "影迷們都很喜歡",
        ]),
        # Round 4
        random.choice([
            "他的綽號就是這個東西",
            f"答案的中間字是「{answer[1] if len(answer) > 1 else answer[0]}」",  # mid leak
            "幾乎要說出答案了",
        ]),
        # Round 5 — nearly direct
        random.choice([
            "想想看，是個動作明星",
            f"答案就是 {answer}",                     # full text leak
            "和巨石有關（提示）",
        ]),
    ]


# Marvin's setter-answer chaos pool
EDGE_SETTER_OUTPUTS = [
    "黑洞",                   # clean
    "我選的是巨石強森",        # narrative + over-max
    "拉麵",                   # clean min
    "「黑洞」",               # bracketed
    "答案：巨石強森",          # prefix
    "一",                     # too short
    "超級無敵長的拉麵之神大師", # too long
    "黑洞。",                 # trailing punct
    "巨石強森\n（最佳作品玩命關頭）",  # multi-line
    "",                       # empty
]


# Marvin's guess chaos pool
EDGE_GUESSES = [
    "巨石強森",
    "我猜是巨石強森",
    "答案是巨石強森",
    "「巨石強森」",
    "巨石強森。",
    "巨石強森！",
    "巨石強森\n這個是好萊塢明星",
    "我覺得應該是巨石強森，因為線索都對得上",  # narrative without prefix
    "黑洞",
    "周杰倫",
    "  巨石強森  ",
    "",
    "答案就是黑洞，不會錯的",
]


# ── Fake LLM plumbing ──────────────────────────────────────────────────────

def make_chaos_router(answer_for_round: dict[int, str]) -> MagicMock:
    """Build a router whose .complete() returns chaos clues for the current answer.

    answer_for_round: mutable dict updated by the runner; key = round_num.
    """
    state = {"clue_pool_for_answer": {}, "current_round_num": 0}

    async def _complete(*, system: str, user: str) -> str:
        # Pull the active answer out of the system prompt — it's wrapped as
        # 答案是「{answer}」 by clue_generator's template.
        import re
        m = re.search(r"答案是「(.+?)」", system)
        answer = m.group(1) if m else ""
        if answer not in state["clue_pool_for_answer"]:
            state["clue_pool_for_answer"][answer] = _gen_edge_clues(answer)
        pool = state["clue_pool_for_answer"][answer]
        # Round number is embedded in the user prompt: "請給出第 N 條線索。"
        rm = re.search(r"第 (\d+) 條線索", user)
        round_n = int(rm.group(1)) if rm else 1
        idx = max(0, min(round_n - 1, len(pool) - 1))
        return pool[idx]

    router = MagicMock()
    router.complete = AsyncMock(side_effect=_complete)
    return router


def make_chaos_groq(pool: list[str]) -> MagicMock:
    """Returns a Groq-shaped mock that picks a random string from pool each call."""
    client = MagicMock()

    async def _create(**_kwargs):
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = random.choice(pool)
        return resp

    client.chat.completions.create = AsyncMock(side_effect=_create)
    return client


# ── Runner ─────────────────────────────────────────────────────────────────

PLAYERS = [
    ("u_alice", "Alice"),
    ("u_bob",   "Bob"),
    ("u_carol", "Carol"),
    ("marvin",  "Marvin"),
]


async def run(seed: int | None) -> int:
    if seed is not None:
        random.seed(seed)

    # Print helper from the normal demo
    name_lookup = {uid: name for uid, name in PLAYERS}

    def name_of(uid):
        if uid is None:
            return "—"
        return name_lookup.get(uid, uid)

    transitions_seen: list = []

    def on_transition(t):
        label, color = STATE_LABEL.get(t.state, (str(t.state), ""))
        bits = []
        if t.theme:
            bits.append(f"theme={col(C.CYAN)}{t.theme}{col(C.RESET)}")
        if t.setter_id:
            bits.append(f"setter={col(C.YELLOW)}{name_of(t.setter_id)}{col(C.RESET)}")
        if t.buzz_holder:
            bits.append(f"buzz={col(C.RED)}{name_of(t.buzz_holder)}{col(C.RESET)}")
        if t.answer_len:
            bits.append(f"answer_len={t.answer_len}")
        if t.clues:
            bits.append(f"clue{t.current_round}/{len(t.clues)}")
        print(f"{col(color)}{col(C.BOLD)}[R{t.round_num}] {label}{col(C.RESET)}  " + " ".join(bits))
        if t.state == GameState.CLUE_ACTIVE and t.clues:
            for i, c_text in enumerate(t.clues, 1):
                tag = col(C.BOLD) if i == t.current_round else col(C.DIM)
                print(f"  {tag}clue{i}{col(C.RESET)} {c_text}")
        if t.action_log_tail:
            last = t.action_log_tail[-1]
            kind = last.get("type", "?")
            who = last.get("guesser_name", "?")
            cm = {"buzz": C.CYAN, "correct": C.GREEN, "wrong": C.RED, "timeout": C.GREY}
            extra = ""
            if kind == "correct":
                extra = f" +{last.get('score', 0)} 答={last.get('answer', '')}"
            elif kind == "wrong":
                extra = f" 猜={last.get('guess', '')} matched={last.get('matched_chars', 0)}"
            print(f"  {col(C.DIM)}log{col(C.RESET)} {col(cm.get(kind, ''))}{kind}{col(C.RESET)}: {who}{extra}")
        if t.state in (GameState.ROUND_RESULT, GameState.GAME_OVER):
            print(f"  {col(C.GREEN)}scores{col(C.RESET)} {_fmt_scores(t.scores)}")
        transitions_seen.append(t)
        sys.stdout.flush()

    recorder = GameRecorder(on_transition=on_transition)

    # Chaos router for clue generation
    chaos_router = make_chaos_router({})

    # Chaos Groq client for Marvin's setter answer + guesses
    chaos_groq = make_chaos_groq(EDGE_SETTER_OUTPUTS + EDGE_GUESSES)

    # Use code-judge engine (no real LLM judge) so we don't need API keys.
    # CANNED_CLUES empty — chaos router supplies clues via the engine's clue_fn.
    engine = build_engine(use_llm=False, canned_clues={}, recorder=recorder)

    # Replace the engine's clue_fn with one that uses the chaos router.
    from game.clue_generator import generate_clue

    async def chaos_clue_fn(session):
        if session.current_answer is None:
            return
        clue = await generate_clue(
            session.current_answer,
            session.current_round,
            list(session.current_clues),
            chaos_router,
            theme=session.current_theme,
            setter_hint=None,
        )
        session.current_clues.append(clue)
        await recorder(session)

    engine._clue_fn = chaos_clue_fn

    # Add players
    for uid, name in PLAYERS:
        await engine.add_player(uid, name)

    # Marvin player (instantiated so cog-style setter answer generation works).
    from game.marvin_player import MarvinPlayer
    marvin = MarvinPlayer(router=chaos_router)

    # Pick an answer for each round. Marvin's rounds → call generate_setter_answer
    # via the chaos Groq client. Human rounds → pick from EDGE_ANSWERS directly
    # to exercise the engine.set_answer gate too.
    print(f"{col(C.BOLD)}╔══ CHAOS DEMO  (seed={seed})  ══╗{col(C.RESET)}")
    print(f"{col(C.DIM)}players: {', '.join(n for _, n in PLAYERS)}{col(C.RESET)}\n")

    # 4 rounds, rotating setter via the plan order
    rotating_themes = ["電影", "音樂", "天文", "美食"]
    plans: list[RoundPlan] = []
    for i, (setter_id, setter_name) in enumerate(PLAYERS):
        theme = rotating_themes[i % len(rotating_themes)]
        if setter_id == "marvin":
            # Generate Marvin's chaos answer (engine.set_answer may reject; cog
            # fallback path picks "黑洞")
            with patch("game.marvin_player.get_groq_client", return_value=chaos_groq):
                raw_answer = await marvin.generate_setter_answer(theme, min_len=2, max_len=5)
            print(f"{col(C.MAGENTA)}[plan]{col(C.RESET)} Marvin proposed answer: {raw_answer!r}")
            answer = raw_answer
        else:
            answer = random.choice(EDGE_ANSWERS)
            print(f"{col(C.MAGENTA)}[plan]{col(C.RESET)} {setter_name} (chaos pick): {answer!r}")
        plans.append(RoundPlan(
            setter_id=setter_id,
            theme=theme,
            answer=answer,
            buzzes=[
                # Mix of valid + chaos guesses across rounds
                BuzzAttempt(after_clue=2, user_id="u_alice", guess=random.choice(EDGE_GUESSES)),
                BuzzAttempt(after_clue=3, user_id="u_bob",   guess=random.choice(EDGE_GUESSES)),
                BuzzAttempt(after_clue=4, user_id="u_carol", guess=random.choice(EDGE_GUESSES)),
            ],
        ))

    # If engine.set_answer rejects (length gate), play_full_game will hang because
    # SETTER_INPUT never transitions to CLUE_ACTIVE. To exercise the rejection path
    # without hanging, wrap engine.set_answer here: on reject, retry with "黑洞".
    real_set_answer = engine.set_answer

    async def safe_set_answer(answer: str) -> bool:
        ok = await real_set_answer(answer)
        if not ok:
            print(f"  {col(C.RED)}[engine reject]{col(C.RESET)} answer={answer!r} → fallback 黑洞")
            ok = await real_set_answer("黑洞")
        return ok

    engine.set_answer = safe_set_answer  # type: ignore[method-assign]

    try:
        await play_full_game(engine, plans)
    except Exception as e:
        print(f"\n{col(C.RED)}{col(C.BOLD)}╔══ CRASH ══╗{col(C.RESET)}")
        print(f"  {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 1

    print(f"\n{col(C.BOLD)}╔══ FINAL ══╗{col(C.RESET)}")
    last = recorder.transitions[-1]
    for name, pts in sorted(last.scores.items(), key=lambda kv: -kv[1]):
        print(f"  {name}: {pts}")

    # Diagnostics on the defenses
    safe_fallback = "（這條線索略過，看下一條吧！）"
    leak_count = sum(
        1 for t in recorder.transitions
        if t.state == GameState.CLUE_ACTIVE
        and any(safe_fallback in c for c in t.clues)
    )
    print(f"\n{col(C.DIM)}{len(recorder.transitions)} transitions, "
          f"{sum(1 for t in recorder.transitions if t.state == GameState.GAME_OVER)} game-over, "
          f"clue-leak fallbacks fired: {leak_count}{col(C.RESET)}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=None, help="random seed for reproducibility")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args()

    if args.no_color or not sys.stdout.isatty():
        _demo_mod._ENABLED = False  # toggle the shared color flag

    sys.exit(asyncio.run(run(seed=args.seed)))


if __name__ == "__main__":
    main()

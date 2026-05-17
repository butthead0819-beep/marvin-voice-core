#!/usr/bin/env python3
"""Busted CLI playback. Run a scripted 4-player game and print every state
transition like a film reel.

Usage:
  python scripts/busted_demo.py                 # code-judge engine (offline, deterministic)
  python scripts/busted_demo.py --llm           # real Cerebras/Groq/Gemini fallback (needs keys)
  python scripts/busted_demo.py --no-color      # plain text, no ANSI
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from game.session import GameState
from scripts.busted_sim_core import (
    BuzzAttempt,
    GameRecorder,
    RoundPlan,
    Transition,
    build_engine,
    play_full_game,
)


# ── ANSI ─────────────────────────────────────────────────────────────────────
class C:
    RESET = "\033[0m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    BLUE = "\033[34m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    MAGENTA = "\033[35m"
    GREY = "\033[90m"


def _strip(_: str) -> str:
    return ""


_ENABLED = True


def col(code: str) -> str:
    return code if _ENABLED else ""


STATE_LABEL = {
    GameState.IDLE:         ("IDLE",         C.GREY),
    GameState.JOINING:      ("JOINING",      C.BLUE),
    GameState.SPINNING:     ("SPINNING",     C.MAGENTA),
    GameState.THEME_SELECT: ("THEME_SELECT", C.CYAN),
    GameState.SETTER_INPUT: ("SETTER_INPUT", C.YELLOW),
    GameState.CLUE_ACTIVE:  ("CLUE_ACTIVE",  C.CYAN),
    GameState.BUZZ_LOCKED:  ("BUZZ_LOCKED",  C.RED),
    GameState.ROUND_RESULT: ("ROUND_RESULT", C.GREEN),
    GameState.GAME_OVER:    ("GAME_OVER",    C.BOLD + C.GREEN),
}


def _fmt_scores(scores: dict[str, int]) -> str:
    parts = [f"{name} {pts}" for name, pts in scores.items()]
    return " · ".join(parts)


def make_printer(name_lookup: dict[str, str]):
    def name_of(uid: str | None) -> str:
        if uid is None:
            return "—"
        return name_lookup.get(uid, uid)

    def print_transition(t: Transition) -> None:
        label, color = STATE_LABEL.get(t.state, (str(t.state), ""))
        header = f"{col(color)}{col(C.BOLD)}[Round {t.round_num}] {label}{col(C.RESET)}"
        bits: list[str] = []
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
        print(header + ("  " + " ".join(bits) if bits else ""))

        # Body
        if t.state == GameState.CLUE_ACTIVE and t.clues:
            for i, c in enumerate(t.clues, 1):
                tag = col(C.BOLD) if i == t.current_round else col(C.DIM)
                print(f"  {tag}clue{i}{col(C.RESET)} {c}")
        if t.wrong_guesses:
            print(f"  {col(C.RED)}❌ wrong{col(C.RESET)} {', '.join(t.wrong_guesses)}")
        if t.action_log_tail:
            last = t.action_log_tail[-1]
            kind = last.get("type", "?")
            who = last.get("guesser_name", "?")
            color_map = {"buzz": C.CYAN, "correct": C.GREEN, "wrong": C.RED, "timeout": C.GREY}
            tag_color = color_map.get(kind, "")
            extra = ""
            if kind == "correct":
                extra = f" +{last.get('score', 0)} · 答={last.get('answer', '?')}"
            elif kind == "wrong":
                extra = f" 猜={last.get('guess', '?')} matched={last.get('matched_chars', 0)}"
            print(f"  {col(C.DIM)}log{col(C.RESET)} {col(tag_color)}{kind}{col(C.RESET)}: {who}{extra}")
        if t.state in (GameState.ROUND_RESULT, GameState.GAME_OVER):
            print(f"  {col(C.GREEN)}scores{col(C.RESET)} {_fmt_scores(t.scores)}")
        sys.stdout.flush()

    return print_transition


# ── Demo scenario ────────────────────────────────────────────────────────────
PLAYERS = [
    ("u_alice", "Alice"),
    ("u_bob",   "Bob"),
    ("u_carol", "Carol"),
    ("u_dave",  "Dave"),
]

# Canned clues per answer — keeps the run reproducible without an LLM.
CANNED_CLUES = {
    "巨石強森": [
        "他是好萊塢動作明星",
        "他從 WWE 職業摔角出身",
        "綽號 The Rock",
        "曾主演玩命關頭系列",
        "本名 Dwayne Johnson",
    ],
    "周杰倫": [
        "他是台灣天王級歌手",
        "外號周董",
        "代表作雙截棍、稻香",
        "妻子是名模昆凌",
        "本名 Jay Chou",
    ],
    "黑洞": [
        "宇宙中最神秘的天體之一",
        "連光都無法逃出",
        "由質量極大的恆星塌縮形成",
        "邊界稱為事件視界",
        "中文兩個字",
    ],
    "拉麵": [
        "一種源自日本的湯麵",
        "湯底常見豚骨、味噌、醬油",
        "配料常有叉燒、蔥花、海苔",
        "札幌、博多都是名店之鄉",
        "中文兩個字",
    ],
}

# Each round: setter, theme, answer, and a list of (after_clue_N, who, what) buzz attempts.
PLAN = [
    RoundPlan(
        setter_id="u_alice", theme="電影", answer="巨石強森",
        buzzes=[
            BuzzAttempt(after_clue=2, user_id="u_bob",   guess="阿諾"),       # wrong
            BuzzAttempt(after_clue=3, user_id="u_carol", guess="巨石強森"),   # correct
        ],
    ),
    RoundPlan(
        setter_id="u_bob", theme="音樂", answer="周杰倫",
        buzzes=[
            BuzzAttempt(after_clue=1, user_id="u_carol", guess="周杰倫"),     # correct R1 (max points)
        ],
    ),
    RoundPlan(
        setter_id="u_carol", theme="天文", answer="黑洞",
        buzzes=[
            BuzzAttempt(after_clue=2, user_id="u_alice", guess="星星"),       # wrong
            BuzzAttempt(after_clue=4, user_id="u_dave",  guess="黑洞"),       # correct R4 (low points)
        ],
    ),
    RoundPlan(
        setter_id="u_dave", theme="美食", answer="拉麵",
        buzzes=[],  # nobody buzzes → round walks through to round 5 → setter penalty
    ),
]


async def run(use_llm: bool) -> None:
    name_lookup = {uid: name for uid, name in PLAYERS}
    recorder = GameRecorder(on_transition=make_printer(name_lookup))
    engine = build_engine(use_llm=use_llm, canned_clues=CANNED_CLUES, recorder=recorder)

    for uid, name in PLAYERS:
        await engine.add_player(uid, name)

    print(f"{col(C.BOLD)}╔══ BUSTED DEMO — {len(PLAYERS)} players, {len(PLAN)} rounds, "
          f"engine={'LLM' if use_llm else 'code-judge'} ══╗{col(C.RESET)}")

    results = await play_full_game(engine, PLAN)

    print(f"\n{col(C.BOLD)}╔══ FINAL ══╗{col(C.RESET)}")
    final_state = recorder.transitions[-1]
    for name, pts in sorted(final_state.scores.items(), key=lambda kv: -kv[1]):
        print(f"  {name}: {pts}")
    print(f"\n{col(C.DIM)}{len(recorder.transitions)} state transitions · "
          f"{sum(1 for r in results if r.get('correct'))} rounds won{col(C.RESET)}")


def main() -> None:
    global _ENABLED
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm", action="store_true", help="Use real LLM judge (needs API keys)")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color")
    args = parser.parse_args()
    if args.no_color or not sys.stdout.isatty():
        _ENABLED = False
    asyncio.run(run(use_llm=args.llm))


if __name__ == "__main__":
    main()

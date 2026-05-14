from __future__ import annotations
import asyncio
import random
import logging
import os
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# Buzz probability by clue round (1-indexed)
BUZZ_PROBABILITY = {1: 0.10, 2: 0.25, 3: 0.50, 4: 0.80, 5: 1.00}

# Rounds 1-3: Marvin only knows the character count — no clues, no context
_GUESS_SYSTEM_BLIND = (
    "你是一個記憶力不太好、有點悲觀的機器人。"
    "你只知道答案的字數，完全沒有任何線索，只能靠直覺亂猜一個詞。"
    "大膽猜測，只說出答案詞，不要解釋。"
)

# Rounds 4-5: Marvin gets the clues AND the list of already-tried wrong answers
_GUESS_SYSTEM_FULL = (
    "你是一個記憶力不太好、有點悲觀的機器人。"
    "根據線索猜一個詞，你的答案可能不對。"
    "絕對不要重複已經猜過的答案。"
    "只說出答案詞，不要解釋。"
)

MARVIN_SETTER_QUIPS = [
    "唉...好吧，換我出題了。反正你們也猜不到。",
    "我來出題。雖然這一切都是徒勞的。",
    "好，看看你們能猜到幾分。我對宇宙的預測是——猜不到。",
    "輪到我了。我選了一個特別難的，因為孤獨是人類的本質。",
    "我出題。如果沒人猜到，我也不感到意外。",
]

MARVIN_CORRECT_QUIPS = [
    "啊，{name} 猜到了。宇宙又少了一個謎。真令人沮喪。",
    "恭喜 {name}。雖然這份喜悅轉瞬即逝。",
    "{name} 猜對了。統計上這是必然的。",
    "沒想到 {name} 真的猜中了。這讓我對人類稍微有點信心，只是一點點。",
    "好吧，{name} 猜到了。至少遊戲還在進行。",
    "我早就知道 {name} 會猜中的。我只是沒說出來。",
    "{name}。對。就是這個答案。你高興嗎？我不怎麼高興。",
]


class MarvinPlayer:
    """Marvin's autonomous player logic. Injected with router and session reference."""

    def __init__(self, router):
        self._router = router
        self._weak_client = AsyncOpenAI(
            api_key=os.getenv("GROQ_API_KEY"),
            base_url="https://api.groq.com/openai/v1",
        )
        self._weak_model = os.getenv("GROQ_SIMPLE_MODEL", "llama-3.1-8b-instant")

    async def should_buzz(self, clue_round: int) -> bool:
        """Returns True if Marvin decides to buzz this round."""
        prob = BUZZ_PROBABILITY.get(clue_round, 1.0)
        return random.random() < prob

    async def generate_guess(
        self,
        clue_round: int,
        clues: list[str],
        char_count: int,
        wrong_guesses: list[str],
    ) -> str:
        """
        Generate Marvin's guess using the weak (Groq) model.

        Rounds 1-3: blind guess — only char_count given.
        Rounds 4-5: full context — clues + already-tried answers to avoid.
        """
        if clue_round <= 3:
            system = _GUESS_SYSTEM_BLIND
            user = f"答案有 {char_count} 個字。請猜出這個詞（只說答案）："
        else:
            system = _GUESS_SYSTEM_FULL
            clue_text = "\n".join(f"線索{i+1}：{c}" for i, c in enumerate(clues))
            avoid_line = (
                f"\n已猜過（不可重複）：{'、'.join(wrong_guesses)}"
                if wrong_guesses
                else ""
            )
            user = (
                f"答案有 {char_count} 個字。\n"
                f"{clue_text}"
                f"{avoid_line}\n"
                "請猜出這個詞（只說答案）："
            )

        try:
            resp = await self._weak_client.chat.completions.create(
                model=self._weak_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=30,
                temperature=1.0,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"[Marvin] Weak model failed: {e}")
            return "我不知道...大概是黑洞？"

    async def think_then_buzz(
        self,
        clue_round: int,
        clues: list[str],
        char_count: int,
        wrong_guesses: list[str],
        on_buzz_ready,
    ) -> None:
        """
        Decides whether to buzz, waits a random delay, then calls on_buzz_ready(guess_text).

        Rounds 1-3: guesses blindly (char count only).
        Rounds 4-5: uses clues and wrong_guesses list to make a more informed (but still weak) guess.

        on_buzz_ready: async callable(guess: str) — called if Marvin decides to buzz.
        This method should be run as a background task.
        """
        if not await self.should_buzz(clue_round):
            return
        delay = random.uniform(1.5, 4.0)
        await asyncio.sleep(delay)
        guess = await self.generate_guess(clue_round, clues, char_count, wrong_guesses)
        await on_buzz_ready(guess)

    def setter_quip(self) -> str:
        return random.choice(MARVIN_SETTER_QUIPS)

    def correct_quip(self, winner_name: str) -> str:
        template = random.choice(MARVIN_CORRECT_QUIPS)
        return template.format(name=winner_name)

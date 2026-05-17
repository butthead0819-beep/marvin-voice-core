from __future__ import annotations
import asyncio
import random
import re
import logging

from game.llm_clients import (
    GROQ_WEAK_MODEL,
    CEREBRAS_MODEL,
    GEMINI_MODEL,
    get_cerebras_client,
    get_groq_client,
    get_gemini_client,
)


_GUESS_PREFIX_RE = re.compile(
    r"^(?:我猜是|我猜|答案是|答案就是|應該是|可能是|大概是|是)\s*"
)
_GUESS_TRAILING_PUNCT = "。.!?！？,，;；:："
_GUESS_BRACKETS = "「」『』《》〈〉【】〔〕[]()（）\"'“”‘’"


def _normalize_guess(raw: str) -> str:
    """Strip LLM verbosity so code-judge can match clean against the answer.

    Handles common patterns the prompt asks the LLM not to do but it does anyway:
      - "我猜是X" / "答案是X" prefixes
      - trailing punctuation (full + half width)
      - wrapping brackets/quotes
      - explanatory second line ("X\n這是好萊塢明星")
    """
    if not raw:
        return ""
    # Keep only first line — explanations typically follow a newline.
    text = raw.splitlines()[0].strip()
    text = _GUESS_PREFIX_RE.sub("", text).strip()
    text = text.strip(_GUESS_BRACKETS).strip()
    text = text.rstrip(_GUESS_TRAILING_PUNCT).strip()
    return text

logger = logging.getLogger(__name__)

# Buzz probability by clue round (1-indexed)
BUZZ_PROBABILITY = {1: 0.10, 2: 0.25, 3: 0.50, 4: 0.80, 5: 1.00}

# Rounds 1-3: Marvin only knows the character count + already-tried wrong answers.
_GUESS_SYSTEM_BLIND = (
    "你是一個記憶力不太好、有點悲觀的機器人。"
    "你只知道答案的字數，以及哪些答案已經被別人猜錯了；沒有其他任何線索。"
    "靠直覺亂猜一個詞，但避開已經猜過的答案。"
    "只說出答案詞，不要解釋。"
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
    """Marvin's autonomous player logic. Injected with router and session reference.

    LLM access goes through game.llm_clients (shared with GameLLMEngine and
    Busted99LLMEngine). Order is Groq weak model (cheapest/fastest) → Cerebras
    → Gemini. Groq used to be a single point of failure; now an outage
    degrades to the next provider instead of breaking Marvin entirely.
    """

    def __init__(self, router):
        self._router = router
        self._last_buzzed_clue_round: int | None = None

    async def should_buzz(self, clue_round: int) -> bool:
        """Returns True if Marvin decides to buzz this round.
        Probability is halved when Marvin buzzed in the immediately preceding clue round.
        """
        prob = BUZZ_PROBABILITY.get(clue_round, 1.0)
        if clue_round > 1 and self._last_buzzed_clue_round == clue_round - 1:
            prob *= 0.5
        return random.random() < prob

    # Per-provider wall-clock cap. Without this a hanging Groq stalls the whole
    # buzz window (Marvin's guess gates a Discord state transition).
    _PROVIDER_TIMEOUT_S = 5.0

    async def _chat_with_fallback(
        self, system: str, user: str, *, max_tokens: int, temperature: float
    ) -> str | None:
        """Groq weak model → Cerebras → Gemini. Returns raw text or None.

        Each provider gets _PROVIDER_TIMEOUT_S seconds before falling through.
        Order is cost/latency-first: Groq llama-8b is cheapest and fastest,
        Cerebras is more capable but slower, Gemini is the paid fallback.
        """
        groq = get_groq_client()
        if groq is not None:
            try:
                resp = await asyncio.wait_for(
                    groq.chat.completions.create(
                        model=GROQ_WEAK_MODEL,
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        max_tokens=max_tokens,
                        temperature=temperature,
                    ),
                    timeout=self._PROVIDER_TIMEOUT_S,
                )
                return resp.choices[0].message.content
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning("[Marvin] Groq failed, fallback Cerebras: %s", e)

        cerebras = get_cerebras_client()
        if cerebras is not None:
            try:
                resp = await asyncio.wait_for(
                    cerebras.chat.completions.create(
                        model=CEREBRAS_MODEL,
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        max_tokens=max_tokens,
                        temperature=temperature,
                    ),
                    timeout=self._PROVIDER_TIMEOUT_S,
                )
                return resp.choices[0].message.content
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning("[Marvin] Cerebras failed, fallback Gemini: %s", e)

        gemini = get_gemini_client()
        if gemini is not None:
            try:
                from google.genai import types
                resp = await asyncio.wait_for(
                    gemini.aio.models.generate_content(
                        model=GEMINI_MODEL,
                        contents=user,
                        config=types.GenerateContentConfig(
                            system_instruction=system,
                            max_output_tokens=max_tokens,
                            temperature=temperature,
                            thinking_config=types.ThinkingConfig(thinking_budget=0),
                        ),
                    ),
                    timeout=self._PROVIDER_TIMEOUT_S,
                )
                return resp.text or None
            except (asyncio.TimeoutError, Exception) as e:
                logger.error("[Marvin] All providers failed: %s", e)

        return None

    async def generate_guess(
        self,
        clue_round: int,
        clues: list[str],
        char_count: int,
        wrong_guesses: list[str],
    ) -> str:
        """
        Generate Marvin's guess via _chat_with_fallback (3-layer LLM chain).

        Rounds 1-3: blind guess — only char_count given.
        Rounds 4-5: full context — clues + already-tried answers to avoid.
        """
        avoid_line = (
            f"\n已猜過（不可重複）：{'、'.join(wrong_guesses)}"
            if wrong_guesses
            else ""
        )
        if clue_round <= 3:
            system = _GUESS_SYSTEM_BLIND
            user = f"答案有 {char_count} 個字。{avoid_line}\n請猜出這個詞（只說答案）："
        else:
            system = _GUESS_SYSTEM_FULL
            clue_text = "\n".join(f"線索{i+1}：{c}" for i, c in enumerate(clues))
            user = (
                f"答案有 {char_count} 個字。\n"
                f"{clue_text}"
                f"{avoid_line}\n"
                "請猜出這個詞（只說答案）："
            )

        raw = await self._chat_with_fallback(system, user, max_tokens=30, temperature=1.0)
        if raw is None:
            return "黑洞"
        return _normalize_guess(raw) or "黑洞"

    async def generate_setter_answer(self, theme: str, min_len: int = 2, max_len: int = 5) -> str:
        """
        Generate an answer related to the theme. Always returns a string in
        [min_len, max_len]; falls back to the theme name or '黑洞' if the LLM
        chain produces nothing usable.
        """
        system = (
            "你是一個在玩猜謎遊戲的機器人，現在輪到你出題。"
            "請想一個跟指定主題相關的具體名詞，只輸出答案詞，不要解釋。"
        )
        user = (
            f"主題：「{theme}」\n"
            f"請給我一個 {min_len} 到 {max_len} 個字的具體名詞答案（只輸出答案詞）："
        )
        raw = await self._chat_with_fallback(system, user, max_tokens=20, temperature=0)
        if raw is not None:
            # Reuse the guess normalizer — strips prefixes, brackets, trailing punct,
            # keeps first line. Then drop spaces / full-width punct.
            answer = _normalize_guess(raw).replace("。", "").replace("，", "").replace(" ", "").strip()
            if min_len <= len(answer) <= max_len:
                return answer
            if len(answer) > max_len:
                return answer[:max_len]
        # All providers failed or output was unusable — fall back to safe default.
        if min_len <= len(theme) <= max_len:
            return theme
        return "黑洞"

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
        self._last_buzzed_clue_round = clue_round
        await on_buzz_ready(guess)

    def setter_quip(self) -> str:
        return random.choice(MARVIN_SETTER_QUIPS)

    def correct_quip(self, winner_name: str) -> str:
        template = random.choice(MARVIN_CORRECT_QUIPS)
        return template.format(name=winner_name)

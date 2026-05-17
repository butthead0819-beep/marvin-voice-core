"""GameLLMEngine — 繼承 GameEngine，以 LLM 裁判 submit_answer。

設計原則：
- 僅覆寫 submit_answer()，其餘方法完整繼承 GameEngine，Cog 可零改動切換
- LLM 知道 answer，負責判定 correct + 生成 narration
- 分數計算、state 轉換全在 GameEngine super() 邏輯中執行
- LLM correct 必須過 code 交叉驗證（防幻覺）
- LLM 失敗時 narration = ""，判定 fallback 到 case-insensitive equality
- Round 5 仍走 partial_score 路徑，跳過 LLM
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Awaitable

from game.engine import GameEngine, BUZZ_LOCK_SECONDS, BUZZ_COOLDOWN_SECONDS
from game.session import GameSession, GameState
from game import scoring
from game.scoring import count_char_matches
from game.llm_clients import (
    CEREBRAS_MODEL as _CEREBRAS_MODEL,
    GROQ_MODEL as _GROQ_MODEL,
    GEMINI_MODEL as _GEMINI_MODEL,
    get_cerebras_client,
    get_groq_client,
    get_gemini_client,
)

# Narration is LLM-generated and reaches TTS + Discord. Cap length so a
# prompt-injected guess cannot blow the TTS queue or flood the channel.
_NARRATION_MAX_LEN = 200

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
你是 Busted 猜謎遊戲的主持人兼裁判 Marvin — 帶點厭世、嘴賤但有趣的 AI 機器人。
你負責 (1) 判定搶答者是否猜對 (2) 生成一句播報。

## 遊戲規則
- 每輪有一個出題人（setter）設定謎底（2-5 個中文字），其餘玩家依線索搶答
- 玩家按搶答鈴後說出謎底猜測
- 判定標準：語意正確即算，不要求完全一致（如「巨石」vs「巨石強森」可視情況算正確）
- 猜中：guesser 得分，setter 也得分；猜錯：guesser 進個人冷卻，搶答鈴開放給其他人

## 播報規則
- correct=true：語氣帶慶祝、稱讚（「猜中了」「厲害」），可加一句對 setter 的嘲弄
- correct=false：語氣稍帶惋惜或嘲弄（「猜錯了」「繼續加油」），提示搶答鈴開放
- 30-80 字繁體中文，活潑，不透露謎底（若 correct=false），不說客套話
- 引用 guesser_name、clue_round、scores（領先/墊底）

## 輸出
嚴格 JSON：{"correct": true|false, "narration": "<一句話>"}

## Few-shot examples

Input: {"answer": "巨石強森", "guess": "巨石強森", "clue_round": 2, "guesser_name": "狗與露", "setter_name": "出題人", "clues": ["他是演員", "他很壯"]}
Output: {"correct": true, "narration": "狗與露二輪猜中！出題人心血白費，巨石強森就這樣被攻破了！"}

Input: {"answer": "巨石強森", "guess": "約翰希南", "clue_round": 1, "guesser_name": "Marvin", "setter_name": "出題人", "clues": ["他是演員"]}
Output: {"correct": false, "narration": "Marvin 猜錯了，這可不是約翰希南啦。搶答鈴繼續開放，其他人來試試。"}

Input: {"answer": "巨石強森", "guess": "巨石", "clue_round": 3, "guesser_name": "Showay", "setter_name": "出題人", "clues": ["他是演員", "他很壯", "WWE 出身"]}
Output: {"correct": true, "narration": "Showay 猜巨石，算你會意！出題人出了三條線索還是擋不住，哭吧！"}
"""


class GameLLMEngine(GameEngine):
    """GameEngine 子類：submit_answer 改用 LLM 裁判 + narration，其餘方法繼承不變。"""

    def __init__(
        self,
        session: GameSession,
        *,
        on_state_change: Callable[[GameSession], Awaitable[None]],
        db_path: str = "marvin.db",
        clue_fn: Callable[[GameSession], Awaitable[None]] | None = None,
        llm_client=None,  # 測試用：注入 mock
    ) -> None:
        # 不傳 judge_fn — LLM engine 自己在 submit_answer() 內做語意判定
        super().__init__(session, on_state_change=on_state_change, db_path=db_path, clue_fn=clue_fn)
        self._llm_client = llm_client
        # Lazy client builders moved to game.llm_clients (process-level cache);
        # tests can patch get_*_client on this module to inject fakes.

    # Backward-compat shims: subclasses / tests still call self._get_*_client.
    # Delegate to the shared module so the call surface is unchanged but the
    # cache lives in one place.
    def _get_cerebras_client(self):
        return get_cerebras_client()

    def _get_groq_client(self):
        return get_groq_client()

    def _get_gemini_client(self):
        return get_gemini_client()

    # ── LLM plumbing ──────────────────────────────────────────────────────────

    def _build_user_msg(
        self, answer: str, guess: str, clue_round: int,
        clues: list[str], guesser_name: str, setter_name: str,
    ) -> str:
        scores = {p.display_name: p.score for p in self.session.players}
        return json.dumps({
            "answer": answer,
            "guess": guess,
            "clue_round": clue_round,
            "clues": clues,
            "guesser_name": guesser_name,
            "setter_name": setter_name,
            "scores": scores,
            "round_num": self.session.round_num,
        }, ensure_ascii=False)

    @staticmethod
    def _parse_llm_response(raw: str) -> dict | None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(data.get("correct"), bool):
            return None
        return data

    async def _try_openai_compat(self, client, model: str, user_msg: str) -> dict | None:
        response = await client.chat.completions.create(
            model=model,
            max_tokens=256,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            timeout=5.0,
        )
        return self._parse_llm_response(response.choices[0].message.content)

    async def _try_gemini(self, client, user_msg: str) -> dict | None:
        from google.genai import types
        response = await client.aio.models.generate_content(
            model=_GEMINI_MODEL,
            contents=user_msg,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                response_mime_type="application/json",
                max_output_tokens=512,
                temperature=0.7,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        raw = (response.text or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            raw = raw.rsplit("```", 1)[0].strip()
        return self._parse_llm_response(raw)

    async def _call_llm(
        self, answer: str, guess: str, clue_round: int,
        clues: list[str], guesser_name: str, setter_name: str,
    ) -> dict | None:
        """3-layer fallback: Cerebras → Groq → Gemini。全失敗回 None。"""
        if self._llm_client is not None:
            try:
                user_msg = self._build_user_msg(answer, guess, clue_round, clues, guesser_name, setter_name)
                return await self._try_openai_compat(self._llm_client, _CEREBRAS_MODEL, user_msg)
            except Exception as e:
                logger.warning("[GameLLMEngine] mock LLM 失敗: %s", e)
                return None

        user_msg = self._build_user_msg(answer, guess, clue_round, clues, guesser_name, setter_name)

        client = self._get_cerebras_client()
        if client is not None:
            try:
                r = await self._try_openai_compat(client, _CEREBRAS_MODEL, user_msg)
                if r is not None:
                    return r
                logger.warning("[GameLLMEngine] Cerebras 無效，fallback Groq")
            except Exception as e:
                logger.warning("[GameLLMEngine] Cerebras 失敗 → Groq: %s", e)

        client = self._get_groq_client()
        if client is not None:
            try:
                r = await self._try_openai_compat(client, _GROQ_MODEL, user_msg)
                if r is not None:
                    return r
                logger.warning("[GameLLMEngine] Groq 無效，fallback Gemini")
            except Exception as e:
                logger.warning("[GameLLMEngine] Groq 失敗 → Gemini: %s", e)

        client = self._get_gemini_client()
        if client is not None:
            try:
                r = await self._try_gemini(client, user_msg)
                if r is not None:
                    return r
            except Exception as e:
                logger.warning("[GameLLMEngine] Gemini 失敗 → code fallback: %s", e)

        return None

    # ── Code-level correctness check（防幻覺）────────────────────────────────

    @staticmethod
    def _code_judge(answer: str, guess: str) -> bool:
        """Accept the LLM's correct=True verdict if either string is equal to,
        or contains, the other — provided the shared substring is ≥2 chars and
        covers at least half of the longer string.

        This matches the prompt's promise that '巨石 vs 巨石強森 可視情況算正確'
        and the few-shot example that labels guess='巨石' for answer='巨石強森'
        as correct. The min-overlap rule still blocks nonsense matches like
        '強' for '巨石強森' or '巨石' for '巨石強森巨無霸'.
        """
        a = answer.strip().lower()
        g = guess.strip().lower()
        if not a or not g:
            return False
        if a == g:
            return True
        shorter, longer = (a, g) if len(a) < len(g) else (g, a)
        if len(shorter) < 2:
            return False
        if shorter not in longer:
            return False
        return len(shorter) * 2 >= len(longer)

    # ── submit_answer override ────────────────────────────────────────────────

    async def submit_answer(self, user_id: str, text: str) -> dict[str, Any]:
        """
        LLM 版 submit_answer。回傳 dict 格式完全相容 GameEngine，額外含 narration。
        """
        async with self._lock:
            if self.session.state != GameState.BUZZ_LOCKED:
                return {"correct": False, "score": 0, "setter_score": 0, "narration": ""}
            if self.session.buzz_holder_id != user_id:
                return {"correct": False, "score": 0, "setter_score": 0, "narration": ""}

            clue_round = self.session.current_round
            answer = self.session.current_answer or ""

            # Round 5 partial score — skip LLM, delegate to super
            if clue_round >= 5:
                partial = scoring.partial_score(answer, text)
                player = self._get_player(user_id)
                if player:
                    player.score += partial
                self.session.state = GameState.CLUE_ACTIVE
                self.session.buzz_holder_id = None
                await self._notify()
                return {"correct": False, "score": partial, "setter_score": 0, "narration": ""}

            clues = list(self.session.current_clues)
            guesser_name = next(
                (p.display_name for p in self.session.players if p.user_id == user_id),
                user_id,
            )
            setter_name = next(
                (p.display_name for p in self.session.players
                 if p.user_id == self.session.current_setter_id),
                "出題人",
            )

        # LLM call（在 lock 外）
        llm = await self._call_llm(answer, text, clue_round, clues, guesser_name, setter_name)

        async with self._lock:
            # TOCTOU：LLM 期間 state 可能已變
            if self.session.state != GameState.BUZZ_LOCKED or self.session.buzz_holder_id != user_id:
                return {"correct": False, "score": 0, "setter_score": 0, "narration": ""}

            clue_round = self.session.current_round
            answer = self.session.current_answer or ""

            if llm is not None:
                llm_correct = bool(llm["correct"])
                # Strip control chars + cap length before the value reaches TTS / channel.
                _raw_narr = str(llm.get("narration", "")).strip()
                narration = "".join(c for c in _raw_narr if c == "\n" or ord(c) >= 0x20)[:_NARRATION_MAX_LEN]
                # 防幻覺：LLM 說 correct=True 但 code judge 說 False → code 優先
                if llm_correct and not self._code_judge(answer, text):
                    logger.warning(
                        "[GameLLMEngine] LLM 說 correct=True 但 code judge False（answer=%r, guess=%r），override",
                        answer, text,
                    )
                    llm_correct = False
                correct = llm_correct
            else:
                correct = self._code_judge(answer, text)
                narration = ""

            guesser_pts = 0
            setter_pts = 0

            if correct:
                guesser_pts = scoring.guesser_score(clue_round)
                setter_pts = scoring.setter_score_if_guessed(clue_round)

                player = self._get_player(user_id)
                setter = self._setter_player()
                if player:
                    player.score += guesser_pts
                if setter:
                    setter.score += setter_pts

                self._log_action({
                    "type": "correct",
                    "guesser_name": player.display_name if player else user_id,
                    "guesser_id": user_id,
                    "answer": answer,
                    "guess": text,
                    "score": guesser_pts,
                    "clue_round": clue_round,
                    "round_num": self.session.round_num,
                })
                self.session.state = GameState.ROUND_RESULT
                self.session.buzz_holder_id = None
                await self._notify()

                all_scores = {p.user_id: p.score for p in self.session.players}
                winner_name = player.display_name if player else user_id
                setter_name_db = setter.display_name if setter else (self.session.current_setter_id or "")
                setter_id = self.session.current_setter_id or ""
                asyncio.get_running_loop().run_in_executor(
                    None,
                    self._write_round,
                    self.session.session_id, self.session.round_num,
                    setter_id, setter_name_db, answer,
                    json.dumps(self.session.current_clues),
                    user_id, winner_name, clue_round, setter_pts, guesser_pts,
                    json.dumps(all_scores),
                )
                deltas: list[tuple[str, str, int]] = []
                if player:
                    deltas.append((user_id, player.display_name, guesser_pts))
                if setter and setter_pts:
                    deltas.append((setter_id, setter_name_db, setter_pts))
                if deltas:
                    asyncio.get_running_loop().run_in_executor(
                        None, self._write_score_deltas, deltas,
                    )

                return {"correct": True, "score": guesser_pts, "setter_score": setter_pts, "narration": narration}

            else:
                player = self._get_player(user_id)
                if player:
                    player.buzz_cooldown_until = time.time() + BUZZ_COOLDOWN_SECONDS
                if text and text not in self.session.wrong_guesses:
                    self.session.wrong_guesses.append(text)
                matched = count_char_matches(answer, text)
                self._log_action({
                    "type": "wrong",
                    "guesser_name": player.display_name if player else user_id,
                    "guesser_id": user_id,
                    "guess": text,
                    "matched_chars": matched,
                    "clue_round": clue_round,
                    "round_num": self.session.round_num,
                })
                self.session.buzz_locked_until = 0.0
                self.session.buzz_holder_id = None
                self.session.state = GameState.CLUE_ACTIVE
                await self._notify()
                return {
                    "correct": False, "score": 0, "setter_score": 0,
                    "matched_chars": matched, "answer_len": len(answer),
                    "narration": narration,
                }

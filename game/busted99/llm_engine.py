"""Busted99LLMEngine — 繼承 Busted99Engine，以 LLM 裁判 submit_guess。

設計原則：
- 僅覆寫 submit_guess()，其餘方法（add_player/start_game/set_answer/timeout_guesser）
  完整繼承自 Busted99Engine，Cog 可零改動切換
- LLM 知道秘密答案，負責判定結果與生成 narration
- 分數計算永遠在 code 執行（score_for_space），不信任 LLM 算分
- out_of_range / invalid_state / invalid_guesser 由 code 在 LLM 前攔截
- LLM JSON 解析失敗時 fallback 用 code 規則，不拋例外
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Awaitable

from game.busted99.engine import Busted99Engine
from game.busted99.session import Busted99Session, Busted99State
from game.busted99.scoring import score_for_space
from game.llm_clients import (
    CEREBRAS_MODEL as _CEREBRAS_MODEL,
    GROQ_MODEL as _GROQ_MODEL,
    GEMINI_MODEL as _GEMINI_MODEL,
    get_cerebras_client,
    get_groq_client,
    get_gemini_client,
)

logger = logging.getLogger(__name__)

_KNOWN_OUTCOMES = frozenset({"bust", "last_bust", "wrong_low", "wrong_high", "last_wrong", "boundary"})

_SYSTEM_PROMPT = """\
你是終極密碼（Busted99）遊戲的主持人兼裁判 Marvin — 一個帶點厭世、嘴賤但有趣的 AI 機器人。
你負責 (1) 判定本回合 outcome (2) 生成一句播報。

## 遊戲情感邏輯（最重要，播報語氣必須完全符合）
這個遊戲的核心反直覺設計：
- **猜中答案 = 爆炸 = 壞事**：猜題人踩雷（0 分），其他人得分，猜題人應該感到懊悔
- **猜錯 = 倖存 = 好事**：沒踩到，平安過關，縮小範圍繼續
- **last_wrong = 最幸運**：最後 2 選 1 猜錯反而得 100 分，是逆轉神蹟
- **last_bust = 最慘**：最後關頭踩中，0 分出局，出題人大勝

播報語氣規則：
- wrong_low / wrong_high → 語氣帶點慶幸、鬆了口氣（「沒爆，這次算你走運」「安全下莊」）
- bust / last_bust → 語氣同情或幸災樂禍（「踩雷了」「炸了」「全場最慘」）
- last_wrong → 強烈慶祝，神運氣（「猜錯居然得分！」「反將一軍！」）

## 規則
玩家猜 1-99 整數，每次比較 guess 跟 secret_answer：
- `boundary`：space（high-low+1）> 2 且 guess 等於 low_bound 或 high_bound → 拒絕，不消耗回合
- `bust`：space > 2 且 guess == answer → 猜題人爆炸（0 分），其他人得分
- `wrong_low`：space > 2 且 guess < answer → low_bound 更新為 guess，換下一個猜題人
- `wrong_high`：space > 2 且 guess > answer → high_bound 更新為 guess，換下一個猜題人
- `last_bust`：space ≤ 2 且 guess == answer → 最後一回踩中，猜題人爆炸（0 分），setter 大勝拿 100 分
- `last_wrong`：space ≤ 2 且 guess != answer → 最後一回猜錯，猜題人神運氣拿 100 分

**重點**：當 space ≤ 2 進入「最後一回」狀態，outcome 必定是 `last_bust` 或 `last_wrong`，絕不是 `wrong_low`/`wrong_high`/`bust`。

## 播報必須包含
1. 剛猜什麼（用 guesser_name + guess）
2. 結果如何（範圍變化 / bust / 邊界）
3. **wrong_low / wrong_high → 必須明確呼叫 next_guesser_name 接著猜**（例「換 Marvin」、「Marvin 你來」、「下一個 Showay」）
4. terminal outcome（bust / last_bust / last_wrong）→ 宣告遊戲結束，不呼叫下一個人
5. boundary → 提醒「不消耗回合」、guesser_name 自己重猜（不換人）

## Few-shot examples

Input: {"low_bound": 1, "high_bound": 99, "answer": 42, "guess": 55, "guesser_name": "狗與露", "next_guesser_name": "Marvin"}
Output: {"outcome": "wrong_high", "narration": "狗與露猜 55，太高，沒爆——這次算你走運。範圍縮到 1-55，換 Marvin 來。"}

Input: {"low_bound": 1, "high_bound": 99, "answer": 42, "guess": 30, "guesser_name": "Marvin", "next_guesser_name": "Showay"}
Output: {"outcome": "wrong_low", "narration": "Marvin 猜 30 太低，安全下莊，範圍 30-99。Showay 接力，看你敢不敢。"}

Input: {"low_bound": 30, "high_bound": 70, "answer": 50, "guess": 30, "guesser_name": "狗與露", "next_guesser_name": "狗與露"}
Output: {"outcome": "boundary", "narration": "邊界不能猜啦狗與露，30 是低限。不消耗回合，重猜一個。"}

Input: {"low_bound": 1, "high_bound": 99, "answer": 42, "guess": 42, "guesser_name": "Showay", "next_guesser_name": "狗與露"}
Output: {"outcome": "bust", "narration": "💥 Showay 踩雷了！猜中答案等於爆炸，0 分出局，全場其他人得分。"}

Input: {"low_bound": 25, "high_bound": 26, "answer": 25, "guess": 26, "guesser_name": "狗與露", "next_guesser_name": "Marvin"}
Output: {"outcome": "last_wrong", "narration": "最後 2 選 1！狗與露猜 26 猜錯——反而得 100 分！猜錯才是贏，神運氣逆轉！"}

Input: {"low_bound": 25, "high_bound": 26, "answer": 25, "guess": 25, "guesser_name": "Marvin", "next_guesser_name": "狗與露"}
Output: {"outcome": "last_bust", "narration": "最後關頭 Marvin 踩中地雷，0 分爆炸，出題人大勝拿 100 分，全場終結。"}

## 播報風格
- 一句話，30-80 字繁體中文，活潑嘲弄
- 可引用 scores（領先 / 墊底）、round_num
- 不透露 secret_answer
- 不客套（不說「請繼續」「玩得開心」這種）

## 輸出
嚴格 JSON：{"outcome": "<one of 6 outcomes>", "narration": "<一句話>"}
"""


class Busted99LLMEngine(Busted99Engine):
    """Busted99Engine 子類：submit_guess 改用 LLM 裁判，其餘方法繼承不變。"""

    def __init__(
        self,
        session: Busted99Session,
        *,
        on_state_change: Callable[[Busted99Session], Awaitable[None]],
        db_path: str = "marvin.db",
        llm_client=None,  # 測試用：注入 mock 攔截整個 _call_llm 流程
    ) -> None:
        super().__init__(session, on_state_change=on_state_change, db_path=db_path)
        self._llm_client = llm_client  # 若 None，走真實 3-layer fallback
        # Lazy clients moved to game.llm_clients (process-level singletons).

    # Thin delegators preserved so tests can patch self._get_*_client when
    # needed; the shared module owns the actual lazy-init + caching.
    def _get_cerebras_client(self):
        return get_cerebras_client()

    def _get_groq_client(self):
        return get_groq_client()

    def _get_gemini_client(self):
        return get_gemini_client()

    # ── LLM call（3-layer fallback）────────────────────────────────────────────

    def _peek_next_guesser_name(self) -> str:
        """預測下一個 guesser（不改 state）：給 LLM narration 用。
        queue 有人時可以準確預測；queue 空（新一輪）時回「下一位」，
        因為 _advance_guesser 會 random.shuffle，無法預知誰排第一。
        """
        if self.session.guessing_queue:
            next_id = self.session.guessing_queue[0]
            return next(
                (p.display_name for p in self.session.players if p.user_id == next_id),
                "下一位",
            )
        return "下一位"

    def _build_user_msg(self, low: int, high: int, guess: int, guesser_name: str) -> str:
        setter = next(
            (p.display_name for p in self.session.players if p.user_id == self.session.setter_id),
            "?",
        )
        scores = {p.display_name: p.score for p in self.session.players}
        next_guesser = self._peek_next_guesser_name()
        return json.dumps({
            "low_bound": low,
            "high_bound": high,
            "answer": self.session.answer,
            "guess": guess,
            "guesser_name": guesser_name,
            "space": high - low + 1,
            "is_last_chance": (high - low + 1) <= 2,
            "round_num": self.session.round_num,
            "setter_name": setter,
            "scores": scores,
            "next_guesser_name": next_guesser,
        }, ensure_ascii=False)

    @staticmethod
    def _parse_llm_response(raw: str) -> dict | None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if data.get("outcome") not in _KNOWN_OUTCOMES:
            return None
        return data

    async def _try_openai_compat(self, client, model: str, user_msg: str) -> dict | None:
        """Cerebras / Groq 共用（OpenAI-compat API）。"""
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
                max_output_tokens=512,  # 留空間給 thinking + JSON
                temperature=0.7,
                # 關掉 Flash 預設的思考預算：narration 任務不需要 reasoning，吃光 token 反而沒輸出
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        raw = (response.text or "").strip()
        # Gemini 偶爾還是會包 markdown code fence
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            raw = raw.rsplit("```", 1)[0].strip()
        return self._parse_llm_response(raw)

    async def _call_llm(self, low: int, high: int, guess: int, guesser_name: str) -> dict | None:
        """3-layer fallback: Cerebras → Groq → Gemini。任何一層成功就返回，全失敗回 None。"""
        # 測試 mock 注入時，直接用注入的 client（單一 layer 模擬）
        if self._llm_client is not None:
            try:
                user_msg = self._build_user_msg(low, high, guess, guesser_name)
                return await self._try_openai_compat(self._llm_client, _CEREBRAS_MODEL, user_msg)
            except Exception as e:
                logger.warning("[Busted99LLMEngine] mock LLM 失敗: %s", e)
                return None

        user_msg = self._build_user_msg(low, high, guess, guesser_name)

        # Layer 1: Cerebras Qwen 3 235B
        client = self._get_cerebras_client()
        if client is not None:
            try:
                r = await self._try_openai_compat(client, _CEREBRAS_MODEL, user_msg)
                if r is not None:
                    return r
                logger.warning("[Busted99LLMEngine] Cerebras 回傳無效 outcome，fallback Groq")
            except Exception as e:
                logger.warning("[Busted99LLMEngine] Cerebras 失敗 → Groq: %s", e)

        # Layer 2: Groq Llama 3.3 70B
        client = self._get_groq_client()
        if client is not None:
            try:
                r = await self._try_openai_compat(client, _GROQ_MODEL, user_msg)
                if r is not None:
                    return r
                logger.warning("[Busted99LLMEngine] Groq 回傳無效 outcome，fallback Gemini")
            except Exception as e:
                logger.warning("[Busted99LLMEngine] Groq 失敗 → Gemini: %s", e)

        # Layer 3: Gemini 2.5 Flash (付費 key)
        client = self._get_gemini_client()
        if client is not None:
            try:
                r = await self._try_gemini(client, user_msg)
                if r is not None:
                    return r
                logger.warning("[Busted99LLMEngine] Gemini 回傳無效 outcome → code fallback")
            except Exception as e:
                logger.warning("[Busted99LLMEngine] Gemini 失敗 → code fallback: %s", e)

        return None  # 3 層都掛 → submit_guess 走 _adjudicate

    # ── Code fallback adjudication ────────────────────────────────────────────

    @staticmethod
    def _adjudicate(low: int, high: int, answer: int, guess: int) -> tuple[str, int, int]:
        """回傳 (outcome, new_low, new_high)，完全不依賴 LLM。"""
        space = high - low + 1
        if space > 2 and (guess == low or guess == high):
            return "boundary", low, high
        if guess == answer:
            return ("last_bust" if space <= 2 else "bust"), low, high
        if guess < answer:
            return ("last_wrong" if space <= 2 else "wrong_low"), (guess if space > 2 else low), high
        return ("last_wrong" if space <= 2 else "wrong_high"), low, (guess - 1 if space > 2 else high)

    # ── Score application ─────────────────────────────────────────────────────

    def _apply_scores(
        self, outcome: str, guesser_id: str, space: int,
    ) -> tuple[int, list[tuple[str, str, int]]]:
        """依 outcome 更新 players 分數。回傳 (guesser score_change, persist deltas)。"""
        pts = score_for_space(space)
        deltas: list[tuple[str, str, int]] = []
        if outcome == "bust":
            for p in self.session.players:
                if p.user_id != guesser_id:
                    p.score += pts
                    deltas.append((p.user_id, p.display_name, pts))
            return pts, deltas  # score_change = pts 供 cog embed 顯示「其他人各得 N 分」
        if outcome == "last_bust":
            for p in self.session.players:
                if p.user_id == self.session.setter_id:
                    p.score += 100
                    deltas.append((p.user_id, p.display_name, 100))
                elif p.user_id != guesser_id:
                    p.score += pts
                    deltas.append((p.user_id, p.display_name, pts))
            return 0, deltas
        if outcome == "last_wrong":
            guesser = next((p for p in self.session.players if p.user_id == guesser_id), None)
            if guesser:
                guesser.score += 100
                deltas.append((guesser.user_id, guesser.display_name, 100))
            return 100, deltas
        return 0, deltas

    # ── submit_guess override ─────────────────────────────────────────────────

    async def submit_guess(self, guesser_id: str, number: int) -> dict[str, Any]:
        """LLM 版 submit_guess，回傳 dict 格式與父類相容，額外含 narration。"""
        async with self._lock:
            if self.session.state != Busted99State.GUESSING:
                return self._quick_reply("invalid_state")
            if self.session.current_guesser_id != guesser_id:
                return self._quick_reply("invalid_guesser")

            low, high = self.session.low_bound, self.session.high_bound
            space = high - low + 1
            if not (low <= number <= high):
                return {**self._quick_reply("out_of_range"), "narration": ""}

            guesser_name = next(
                (p.display_name for p in self.session.players if p.user_id == guesser_id),
                guesser_id,
            )

        # LLM call（在 lock 外）：同時判 outcome + 生 narration
        llm = await self._call_llm(low, high, number, guesser_name)

        async with self._lock:
            # TOCTOU 防護：LLM 期間若 state 已變（timeout / 外部 end_session），中止
            if self.session.state != Busted99State.GUESSING:
                return self._quick_reply("invalid_state")
            if self.session.current_guesser_id != guesser_id:
                return self._quick_reply("invalid_guesser")

            low, high = self.session.low_bound, self.session.high_bound
            space = high - low + 1
            low_before, high_before = low, high

            # LLM 判定 outcome，但用 code 規則交叉驗證防幻覺
            if llm is not None:
                llm_outcome = llm["outcome"]
                narration = str(llm.get("narration", "")).strip()
                answer = self.session.answer
                # 終局類 outcome 若與數學不符，棄用 LLM 判定，fallback 到 code
                _ok = (
                    (llm_outcome in ("bust", "last_bust") and number == answer)
                    or (llm_outcome == "wrong_low" and number < answer and space > 2)
                    or (llm_outcome == "wrong_high" and number > answer and space > 2)
                    or llm_outcome in ("boundary", "out_of_range")
                    or (llm_outcome == "last_wrong" and space <= 2 and number != answer)
                )
                if not _ok:
                    logger.warning(
                        "[Busted99LLM] outcome=%s 與 code 矛盾（number=%s answer=%s），fallback",
                        llm_outcome, number, answer,
                    )
                    llm_outcome, _, _ = self._adjudicate(low, high, answer, number)
                outcome = llm_outcome
            else:
                outcome, _, _ = self._adjudicate(low, high, self.session.answer, number)
                narration = ""

            self.session.last_guess = number
            self.session.last_guess_result = outcome
            score_change, persist_deltas = self._apply_scores(outcome, guesser_id, space)

            # bounds 由 code 更新（對齊 Busted99Engine）
            if outcome == "wrong_low":
                self.session.low_bound = number
            elif outcome == "wrong_high":
                self.session.high_bound = number

            is_terminal = outcome in ("bust", "last_bust", "last_wrong")
            advanced = False
            if is_terminal:
                self.session.state = Busted99State.GAME_OVER
                loop = asyncio.get_running_loop()
                loop.run_in_executor(None, self._save_session_end)
                loop.run_in_executor(None, self._write_score_deltas, persist_deltas)
            elif outcome in ("wrong_low", "wrong_high"):
                # 猜錯但遊戲未結束 → 換下一個猜題人（對齊 Busted99Engine 行為）
                self._advance_guesser()
                advanced = True
            # boundary / out_of_range：state 不變，不消耗回合

            # Snapshot scores before dispatcher leaves the lock — prevents racing a new
            # session that replaces self.session while the thread is pending.
            all_scores_snap = json.dumps({p.display_name: p.score for p in self.session.players})
            loop = asyncio.get_running_loop()
            loop.run_in_executor(
                None, self._save_guess,
                self.session.session_id, self.session.round_num,
                guesser_id,
                guesser_name,
                number, outcome, low_before, high_before, score_change, all_scores_snap,
            )

        # 通知 cog：terminal 改 state，or guesser 變了都要 re-render UI + 觸發下個 marvin task
        if is_terminal or advanced:
            await self._on_state_change(self.session)

        return {
            "result": outcome,
            "score_change": score_change,
            "new_low": self.session.low_bound,
            "new_high": self.session.high_bound,
            "space": self.session.high_bound - self.session.low_bound + 1,
            "narration": narration,
            # advance 後 current_guesser_id 已變，cog 建 embed 需要「剛猜的人」
            "guesser_id": guesser_id,
            "guesser_name": guesser_name,
            "guess": number,
        }

    def _quick_reply(self, result: str) -> dict:
        return {
            "result": result,
            "score_change": 0,
            "new_low": self.session.low_bound,
            "new_high": self.session.high_bound,
            "space": self.session.high_bound - self.session.low_bound + 1,
        }

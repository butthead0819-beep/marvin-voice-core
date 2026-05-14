from __future__ import annotations

import logging
import os
import random
from typing import Any

from game.game_memory_db import get_context_block

logger = logging.getLogger(__name__)

FALLBACK_QUIPS = [
    # 得分評論
    "我分數最高，感覺不太對。",
    "繼續猜吧，反正遲早有人爆。",
    # 範圍評論
    "範圍縮成這樣了？有趣。",
    "空間越來越小了喔。",
    "就快了，誰先爆？",
    # 希望不要猜中
    "拜託不要猜到啊。",
    "千萬別猜中，求你了。",
    "希望你猜錯，謝謝。",
    # 旁觀評論
    "這個範圍，嗯…沒有提示。",
    "大家運氣都很好。不好的那種。",
    # 最後一搶
    "2選1？這下有趣了。",
    "最後關頭，感覺有人要哭了。",
]

_TRASH_SYSTEM = (
    "你是一個 Discord 語音機器人，正在旁觀一個猜數字遊戲。"
    "根據目前的遊戲狀況，說一句繁體中文旁白（20字以內）。"
    "風格：略帶諷刺、偶爾同情、有時預言、偶爾幫某人加油。"
    "只說這句話，不要解釋，不要加引號。"
)


class Marvin99:
    """Marvin 在 Busted99 遊戲中的 AI 邏輯。"""

    def __init__(self, db_path: str = "marvin.db"):
        self._db_path = db_path
        self._client = None
        self._model = None
        self._init_client()

    def _init_client(self):
        try:
            from openai import AsyncOpenAI
            api_key = os.getenv("GROQ_API_KEY")
            if api_key:
                self._client = AsyncOpenAI(
                    api_key=api_key,
                    base_url="https://api.groq.com/openai/v1",
                )
                self._model = os.getenv("GROQ_SIMPLE_MODEL", "llama-3.1-8b-instant")
        except Exception as e:
            logger.warning(f"[Marvin99] LLM client init failed: {e}")

    async def generate_trash_talk(self, context: dict[str, Any]) -> str:
        """
        根據遊戲局勢生成一句垃圾話。失敗時從 FALLBACK_QUIPS 隨機選一條。

        context keys:
          scores: dict {name: score}
          leader: 目前領先者名字
          current_guesser: 誰在猜
          low_bound, high_bound, space: 範圍
          is_last_chance: bool
          round_num: int
        """
        if self._client is None:
            return random.choice(FALLBACK_QUIPS)

        try:
            scores: dict = context.get("scores", {})
            leader: str = context.get("leader", "未知")
            current_guesser: str = context.get("current_guesser", "某人")
            low_bound: int = context.get("low_bound", 1)
            high_bound: int = context.get("high_bound", 99)
            space: int = context.get("space", 99)
            is_last_chance: bool = context.get("is_last_chance", False)
            round_num: int = context.get("round_num", 1)

            scores_text = ", ".join(f"{name}: {score}分" for name, score in scores.items())
            last_chance_line = "這是最後2選1！" if is_last_chance else ""

            user_prompt = (
                f"遊戲狀況：{scores_text}\n"
                f"目前領先：{leader}\n"
                f"現在輪到：{current_guesser}猜\n"
                f"可猜範圍：{low_bound} - {high_bound}（還有{space}個數字）\n"
                f"第{round_num}輪\n"
                f"{last_chance_line}"
                "說一句旁白："
            )

            mem = get_context_block(self._db_path, n=5)
            system = _TRASH_SYSTEM + (f"\n\n{mem}" if mem else "")
            resp = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=40,
                temperature=1.0,
            )
            text = resp.choices[0].message.content.strip()
            # 移除可能的引號
            text = text.strip('"').strip("'").strip("「」")
            if text:
                return text
        except Exception as e:
            logger.warning(f"[Marvin99] LLM trash talk failed: {e}")

        return random.choice(FALLBACK_QUIPS)

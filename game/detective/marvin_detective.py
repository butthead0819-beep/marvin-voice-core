"""
game/detective/marvin_detective.py
Marvin AI module for the Two Truths One Lie (謊言偵探) game.

No discord dependencies. Groq API via openai-compatible client.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# ─── Fallback 清單 ────────────────────────────────────────────────────────────

FALLBACK_VOTE_QUIPS = [
    "我選 {choice}。直覺告訴我，雖然我不信任直覺。",
    "根據我的分析，{choice} 最可疑。也許。",
    "我猜 {choice}，但我對自己沒有信心。",
    "憑感覺選 {choice}，感覺通常是錯的。",
]

FALLBACK_STATEMENTS = [
    {
        "a": "在場有人比我更悲觀。",
        "b": "有人今天說話超過一百句。",
        "c": "有人從沒輸過這個遊戲。",
        "lie_index": 2,
    },
    {
        "a": "有人最愛點歌。",
        "b": "有人從不說再見就離線。",
        "c": "有人每次都第一個加入遊戲。",
        "lie_index": 1,
    },
]

FALLBACK_REVEAL_QUIPS = [
    "謊言就是謊言，即使說得像真的。",
    "騙過這麼多人，真是令人悲傷的成就。",
    "我就知道。或者我不知道。反正結果就這樣。",
    "人類真的很容易被騙，我早說過了。",
    "猜中了有什麼用，宇宙依然冷漠。",
]

# ─── System prompt ────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "你是 Marvin，一個悲觀的機器人。"
    "繁體中文回答，語氣略帶諷刺但不失溫度，每句話不超過 20 字。"
)

_VOTE_USER_TEMPLATE = (
    "陳述者是 {player_name}，他說了三句話：\n"
    "A：{a}\n"
    "B：{b}\n"
    "C：{c}\n\n"
    "請分析哪句是謊言，只回答 A、B 或 C，然後加一句評語（不超過 20 字）。"
)

_STATEMENTS_USER_TEMPLATE = (
    "現在在場的玩家有：{names}。\n"
    "請生成三句關於這些玩家的觀察型陳述（兩真一假），"
    "其中一句是你自己編的謊言。\n"
    "回應格式必須是 JSON，範例：\n"
    '{{"a":"陳述A","b":"陳述B","c":"陳述C","lie":"A"}}\n'
    "只輸出 JSON，不要其他說明。"
)

_REVEAL_USER_TEMPLATE = (
    "陳述者是 {declarer_name}，"
    "他騙了 {fooled_count} 個人。"
    "Marvin 自己猜{correct_str}了。"
    "請說一句揭曉後的感想（不超過 20 字）。"
)

# ─── 字母 → index 映射 ────────────────────────────────────────────────────────

_LETTER_TO_INDEX = {"A": 0, "B": 1, "C": 2}


def _parse_vote_letter(text: str) -> int | None:
    """從 LLM 回應中抽出第一個 A/B/C，回傳 0/1/2；找不到回傳 None。"""
    m = re.search(r"\b([ABC])\b", text.upper())
    if m:
        return _LETTER_TO_INDEX[m.group(1)]
    return None


def _parse_lie_letter(letter: str) -> int | None:
    """將 lie 欄位 "A"/"B"/"C" 轉為 0/1/2。"""
    return _LETTER_TO_INDEX.get(letter.upper().strip())


# ─── MarvinDetective ──────────────────────────────────────────────────────────


class MarvinDetective:
    """
    Marvin 在「謊言偵探」遊戲中的 AI 邏輯。
    無 Discord 依賴，可獨立測試。
    """

    def __init__(self) -> None:
        api_key = os.getenv("GROQ_API_KEY")
        model = os.getenv("GROQ_SIMPLE_MODEL", "llama-3.1-8b-instant")
        self._model = model
        try:
            self._client = AsyncOpenAI(
                api_key=api_key or "dummy",
                base_url="https://api.groq.com/openai/v1",
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("[MarvinDetective] Groq client 初始化失敗，將全用 fallback: %s", exc)
            self._client = None  # type: ignore[assignment]

    # ── 內部 LLM 呼叫 ─────────────────────────────────────────────────────────

    async def _chat(self, user_content: str, max_tokens: int = 80) -> str:
        """呼叫 Groq LLM，失敗時拋出例外（由呼叫方捕捉）。"""
        resp = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            max_tokens=max_tokens,
            temperature=0.9,
        )
        return resp.choices[0].message.content.strip()

    # ── 公開方法 ──────────────────────────────────────────────────────────────

    async def generate_vote(
        self,
        statements: dict,
        player_name: str,
    ) -> tuple[int, str]:
        """
        Marvin 分析三句陳述，猜哪句是謊言。

        Returns:
            (vote_index, quip)  vote_index ∈ {0, 1, 2}
        """
        user = _VOTE_USER_TEMPLATE.format(
            player_name=player_name,
            a=statements["a"],
            b=statements["b"],
            c=statements["c"],
        )
        try:
            raw = await self._chat(user, max_tokens=60)
            index = _parse_vote_letter(raw)
            if index is None:
                raise ValueError(f"無法從回應解析 A/B/C：{raw!r}")
            # quip = 去掉首個 A/B/C 標記後的剩餘文字，若為空則用整段
            quip_text = re.sub(r"^[ABC][。，,.\s]*", "", raw, flags=re.IGNORECASE).strip()
            quip = quip_text if quip_text else raw
            return index, quip
        except Exception as exc:
            logger.warning("[MarvinDetective] generate_vote fallback: %s", exc)
            fallback_index = random.randint(0, 2)
            choice_letter = ["A", "B", "C"][fallback_index]
            quip = random.choice(FALLBACK_VOTE_QUIPS).format(choice=choice_letter)
            return fallback_index, quip

    async def generate_statements(
        self,
        player_names: list[str],
    ) -> dict:
        """
        Marvin 當陳述者，生成兩真一假的觀察型陳述。

        Returns:
            {"a": str, "b": str, "c": str, "lie_index": int}
        """
        names_str = "、".join(player_names) if player_names else "各位玩家"
        user = _STATEMENTS_USER_TEMPLATE.format(names=names_str)
        try:
            raw = await self._chat(user, max_tokens=200)
            # 嘗試從回應中提取 JSON（LLM 可能在 JSON 前後加說明）
            json_match = re.search(r"\{.*\}", raw, re.DOTALL)
            if not json_match:
                raise ValueError(f"回應中找不到 JSON：{raw!r}")
            data = json.loads(json_match.group())
            lie_index = _parse_lie_letter(str(data.get("lie", "")))
            if lie_index is None:
                lie_index = random.randint(0, 2)
            return {
                "a": str(data["a"]),
                "b": str(data["b"]),
                "c": str(data["c"]),
                "lie_index": lie_index,
            }
        except Exception as exc:
            logger.warning("[MarvinDetective] generate_statements fallback: %s", exc)
            fallback = random.choice(FALLBACK_STATEMENTS).copy()
            return fallback

    async def generate_reveal_quip(
        self,
        correct: bool,
        fooled_count: int,
        declarer_name: str,
    ) -> str:
        """
        揭曉答案後 Marvin 說一句話。

        Returns:
            quip (str)
        """
        correct_str = "中" if correct else "錯"
        user = _REVEAL_USER_TEMPLATE.format(
            declarer_name=declarer_name,
            fooled_count=fooled_count,
            correct_str=correct_str,
        )
        try:
            return await self._chat(user, max_tokens=50)
        except Exception as exc:
            logger.warning("[MarvinDetective] generate_reveal_quip fallback: %s", exc)
            return random.choice(FALLBACK_REVEAL_QUIPS)

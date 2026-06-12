"""
mood_sensor.py — Phase 1 M2: current_vibe() API

從房間對話 + 既有 ambient intelligence signal 推 mood label。
LLM 只在這裡跑（每 round 結束 invalidate cache 後重算）。

Architecture (per design doc Phase 1):
    Chat stream (TranscriptStore last 5min)
      ↓
    [Groq llama-3.1-8b-instant] mood classifier (4 檔)
      ↓
    {mood: 放鬆/興奮/低落/分歧, topic, engagement, ...}

Cache: 5 min TTL，每 round 結束由 voice_controller 呼 invalidate()。
Fallback: LLM fail 用 stale cache; 連續 3 fail 回 default。Never raise。

Engagement 維度永遠取自 DiscordTemperatureMonitor.temperature（真實值，
即使 LLM fail 也準）。Mood + topic 才依賴 LLM。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Literal, Optional

logger = logging.getLogger(__name__)

# ── 常數 ─────────────────────────────────────────────────────────────────────

MOOD_LABELS = ("放鬆", "興奮", "低落", "分歧")
DEFAULT_MOOD: Literal["放鬆", "興奮", "低落", "分歧"] = "放鬆"
CACHE_TTL_S = 5 * 60
WINDOW_S = 5 * 60                  # 看過去多少對話
MIN_TRANSCRIPTS_FOR_LLM = 2        # 對話 < 此數 → 直接回 default 不浪費 LLM call
MAX_CONSECUTIVE_FAILS = 3

MOOD_CLASSIFIER_MODEL = "llama-3.1-8b-instant"
MOOD_CLASSIFIER_TIMEOUT_S = 5.0
MOOD_CLASSIFIER_MAX_TOKENS = 10

# Mood classifier prompt — 與 scripts/phase0_mood_flip.py 同步維護
MOOD_CLASSIFIER_SYSTEM = """你是房間 vibe 分類器。任務：讀一段 Discord 多人對話片段，分類到一個 mood label。

4 種 mood:
- 放鬆：緩和閒聊、無明顯情緒起伏、日常話題
- 興奮：笑、驚訝、好玩、節奏快、互相吐槽熱絡
- 低落：抱怨、累、煩躁、話少、低能量
- 分歧：多人情緒不一致、爭論、有人嗨有人冷、話不投機

只輸出一個詞 (放鬆/興奮/低落/分歧)，不要其他文字、不要標點。"""


# ── DataClass ────────────────────────────────────────────────────────────────

@dataclass
class VibeLabel:
    mood: str                            # 放鬆 / 興奮 / 低落 / 分歧
    topic: str                           # 短語、空字串若無
    engagement: float                    # 0.0-1.0+ (temperature_monitor 來)
    timestamp: float
    speakers_at_sample: list[str] = field(default_factory=list)
    source: str = "llm"                  # "llm" / "stale_cache" / "default_fallback" / "default_no_convo"


def _default_label(engagement: float = 0.5, source: str = "default_fallback") -> VibeLabel:
    return VibeLabel(
        mood=DEFAULT_MOOD,
        topic="",
        engagement=engagement,
        timestamp=time.time(),
        speakers_at_sample=[],
        source=source,
    )


# ── Mood label 解析 ──────────────────────────────────────────────────────────

def parse_mood_label(text: str) -> Optional[str]:
    """從 LLM raw response 容錯抽 mood label。無法解析回 None。"""
    if not text:
        return None
    for label in MOOD_LABELS:
        if label in text:
            return label
    return None


# ── MoodSensor ───────────────────────────────────────────────────────────────

class MoodSensor:
    """
    Cache + fallback wrapper around mood classifier LLM call.

    Args:
        transcript_store: TranscriptStore instance
        groq_client: AsyncGroq client (or compatible mock)
        temperature_monitor: DiscordTemperatureMonitor (for engagement 真值)
    """

    def __init__(self, transcript_store, groq_client, temperature_monitor, router=None):
        self._transcripts = transcript_store
        self._groq = groq_client
        self._temp = temperature_monitor
        # router 有 → 走 LLM Bus；無 → groq 直打（測試相容）
        self._router = router

        self._cache: Optional[VibeLabel] = None
        self._cache_until: float = 0.0
        self._lock = asyncio.Lock()
        self._consecutive_fails = 0

    async def current_vibe(self, guild_id: int, force_refresh: bool = False) -> VibeLabel:
        """
        回當前房間 vibe。Cache 5min TTL。force_refresh / cache 過期 → 重算。
        任何錯誤都回 fallback、never raise。
        """
        now = time.time()
        if not force_refresh and self._cache is not None and now < self._cache_until:
            return self._cache

        async with self._lock:
            # double-check after lock acquire (避免並發 race)
            now = time.time()
            if not force_refresh and self._cache is not None and now < self._cache_until:
                return self._cache

            label = await self._compute_label(guild_id)
            self._cache = label
            self._cache_until = now + CACHE_TTL_S
            return label

    def invalidate(self) -> None:
        """voice_controller 每 round 結束呼一次，下次 current_vibe() 強制重算。"""
        self._cache_until = 0.0

    # ── 私有 ─────────────────────────────────────────────────────────────────

    async def _compute_label(self, guild_id: int) -> VibeLabel:
        # 1. engagement 永遠用 temperature_monitor 真實值（不依賴 LLM）
        engagement = self._safe_engagement()

        # 2. 取近 5min transcript
        try:
            recent = self._transcripts.get_recent(
                speaker=None, guild_id=guild_id, minutes=WINDOW_S // 60,
            )
        except Exception:
            logger.exception("[MoodSensor] transcript_store 失敗")
            return self._fallback(engagement, reason="transcript_fail")

        # 3. 對話太少 → 直接回 default (不浪費 LLM call)
        if len(recent) < MIN_TRANSCRIPTS_FOR_LLM:
            self._consecutive_fails = 0  # 這不算 LLM fail
            return VibeLabel(
                mood=DEFAULT_MOOD, topic="", engagement=engagement,
                timestamp=time.time(),
                speakers_at_sample=list({r["speaker"] for r in recent}),
                source="default_no_convo",
            )

        # 4. 跑 LLM
        try:
            mood = await self._classify_mood(recent)
        except Exception as e:
            logger.warning(f"[MoodSensor] LLM 失敗: {e}")
            return self._fallback(engagement, reason="llm_fail")

        # 5. LLM 成功
        self._consecutive_fails = 0
        return VibeLabel(
            mood=mood,
            topic="",   # v1 不從 LLM 抓 topic（避免 prompt 變複雜）；v2 加
            engagement=engagement,
            timestamp=time.time(),
            speakers_at_sample=list({r["speaker"] for r in recent}),
            source="llm",
        )

    def _safe_engagement(self) -> float:
        try:
            return float(self._temp.temperature)
        except Exception:
            return 0.5

    def _fallback(self, engagement: float, reason: str) -> VibeLabel:
        """LLM / transcript fail → 用 stale cache（若有），否則 default_fallback。"""
        self._consecutive_fails += 1
        if self._cache is not None and self._consecutive_fails < MAX_CONSECUTIVE_FAILS:
            # Stale cache 還算可用、但更新 engagement (真值)
            stale = VibeLabel(
                mood=self._cache.mood,
                topic=self._cache.topic,
                engagement=engagement,
                timestamp=time.time(),
                speakers_at_sample=self._cache.speakers_at_sample,
                source="stale_cache",
            )
            return stale
        return _default_label(engagement=engagement, source="default_fallback")

    async def _classify_mood(self, transcripts: list[dict]) -> str:
        """跑一次 Groq classifier。失敗 raise。回 4 檔 mood label。"""
        # 組 user prompt
        lines = [f"{t['speaker']}: {t['text']}" for t in transcripts]
        user_prompt = "對話片段（5 分鐘窗口）：\n" + "\n".join(lines)

        if self._router is not None:
            content = await asyncio.wait_for(
                self._router._call_llm(MOOD_CLASSIFIER_SYSTEM, user_prompt,
                                       tier="simple", temperature=0.2,
                                       # 顯式 purpose：被 asyncio.wait_for 包住時 frame 自動歸因會誤記 "wait_for"
                                       purpose="_classify_mood"),
                timeout=MOOD_CLASSIFIER_TIMEOUT_S,
            )
            content = (content or "").strip()
        else:
            resp = await asyncio.wait_for(
                self._groq.chat.completions.create(
                    model=MOOD_CLASSIFIER_MODEL,
                    messages=[
                        {"role": "system", "content": MOOD_CLASSIFIER_SYSTEM},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.2,
                    max_tokens=MOOD_CLASSIFIER_MAX_TOKENS,
                    stream=False,
                ),
                timeout=MOOD_CLASSIFIER_TIMEOUT_S,
            )
            content = resp.choices[0].message.content.strip()
        mood = parse_mood_label(content)
        if mood is None:
            raise ValueError(f"無法解析 mood: {content!r}")
        return mood

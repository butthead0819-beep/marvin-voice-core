"""RoomMoodState — 三個核心 social agent 共用的房間狀態。

設計來源：docs/social_catalyst_plan.md（Week 1 基建）。

寫者：
  - MoodAgent → individual_mood / group_mood / group_temperature
  - DuckingAgent → hot_chat / hot_chat_pair

讀者：所有 SpeakBus agent

不變式：
  - 寫入永遠更新 updated_at（讓 caller 能做 staleness 判斷）
  - load 失敗（檔案不在 / 壞 JSON）絕對不該 raise——回 fallback 預設值
  - dump 是 best-effort，失敗只 log 不傳播
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

MoodLabel = Literal["放鬆", "興奮", "低落", "分歧"]
DEFAULT_MOOD: MoodLabel = "放鬆"


@dataclass
class RoomMoodState:
    channel_id: int
    individual_mood: dict[str, str] = field(default_factory=dict)
    group_mood: str = DEFAULT_MOOD
    group_temperature: float = 0.0
    hot_chat: bool = False
    hot_chat_pair: tuple[str, str] | None = None
    updated_at: float = 0.0


class RoomMoodStateStore:
    """In-memory store keyed by channel_id, with JSON dump for restart resilience."""

    def __init__(self, dump_path: str = "data/room_mood_state.json") -> None:
        self._dump_path = dump_path
        self._states: dict[int, RoomMoodState] = {}

    # ── read path ────────────────────────────────────────────────────────────

    def get(self, channel_id: int) -> RoomMoodState:
        """讀 state。不存在則回 default（不寫入 store，避免無謂膨脹）。"""
        return self._states.get(channel_id) or RoomMoodState(channel_id=channel_id)

    # ── write path ───────────────────────────────────────────────────────────

    def set_individual_mood(self, channel_id: int, speaker: str, mood: str) -> None:
        state = self._states.setdefault(channel_id, RoomMoodState(channel_id=channel_id))
        state.individual_mood[speaker] = mood
        state.updated_at = time.time()

    def set_group(
        self,
        channel_id: int,
        *,
        mood: str | None = None,
        temperature: float | None = None,
    ) -> None:
        state = self._states.setdefault(channel_id, RoomMoodState(channel_id=channel_id))
        if mood is not None:
            state.group_mood = mood
        if temperature is not None:
            state.group_temperature = float(temperature)
        state.updated_at = time.time()

    def set_hot_chat(
        self,
        channel_id: int,
        *,
        hot: bool,
        pair: tuple[str, str] | None = None,
    ) -> None:
        state = self._states.setdefault(channel_id, RoomMoodState(channel_id=channel_id))
        state.hot_chat = hot
        state.hot_chat_pair = pair if hot else None
        state.updated_at = time.time()

    # ── persistence ──────────────────────────────────────────────────────────

    def dump(self) -> None:
        """Best-effort dump to JSON. 失敗只 log。"""
        try:
            payload = {
                str(cid): {
                    "channel_id": s.channel_id,
                    "individual_mood": s.individual_mood,
                    "group_mood": s.group_mood,
                    "group_temperature": s.group_temperature,
                    "hot_chat": s.hot_chat,
                    "hot_chat_pair": list(s.hot_chat_pair) if s.hot_chat_pair else None,
                    "updated_at": s.updated_at,
                }
                for cid, s in self._states.items()
            }
            path = Path(self._dump_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
            tmp.replace(path)
        except Exception as e:
            logger.warning("[RoomMoodState] dump failed: %s", e)

    def load(self) -> None:
        """Best-effort load. 檔案缺失 / 壞 JSON 都 swallow。"""
        try:
            path = Path(self._dump_path)
            if not path.exists():
                return
            raw = path.read_text()
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("[RoomMoodState] load failed (%s) — using defaults", e)
            return

        for _, entry in data.items():
            try:
                cid = int(entry["channel_id"])
                pair = entry.get("hot_chat_pair")
                state = RoomMoodState(
                    channel_id=cid,
                    individual_mood=dict(entry.get("individual_mood") or {}),
                    group_mood=entry.get("group_mood", DEFAULT_MOOD),
                    group_temperature=float(entry.get("group_temperature", 0.0)),
                    hot_chat=bool(entry.get("hot_chat", False)),
                    hot_chat_pair=tuple(pair) if pair else None,
                    updated_at=float(entry.get("updated_at", 0.0)),
                )
                self._states[cid] = state
            except (KeyError, ValueError, TypeError) as e:
                logger.warning("[RoomMoodState] skip bad entry: %s", e)
                continue

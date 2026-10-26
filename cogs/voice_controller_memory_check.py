"""🧠 [MemoryCheck] 換歌空檔問一句記憶確認接線（核心在 memory_check.py）。

房間長時間沒人講話 → 換歌空檔改問一句「你還喜歡 X 嗎？」取代那次 DJ 口白。
回答只收被問的人短窗口內第一句，不需喚醒詞，只解讀對/不對/聽不懂，不當指令。
2026-10-26 使用者定案，見 handle_stt_result（估停之後）與 music_cog_tail_dj 接點。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from memory_check import (
    ANSWER_WINDOW_S,
    CONFIRM_DELTA,
    answer_window_open,
    artist_names,
    classify_answer,
    pick_question,
    question_text,
)
from suki_memory import is_pseudo_player
from tts_speak_policy import SpeakKind

logger = logging.getLogger(__name__)

_LOG_PATH = "records/memory_checks.jsonl"
_FINGERPRINT_PATH = "records/taste_fingerprint.json"


def _append_memory_check_log(row: dict) -> None:
    """事件當下 append 一行；IO 失敗靜默略過（同 quality_metrics.record_metric 慣例）。"""
    try:
        p = Path(_LOG_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _load_memory_check_log() -> list[dict]:
    p = Path(_LOG_PATH)
    if not p.exists():
        return []
    rows: list[dict] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(row, dict):
                rows.append(row)
    except Exception:
        return rows
    return rows


def _load_artist_names() -> set:
    try:
        with open(_FINGERPRINT_PATH, encoding="utf-8") as f:
            fingerprint = json.load(f)
    except Exception:
        return set()
    return artist_names(fingerprint)


class MemoryCheckMixin:
    async def maybe_ask_memory_check(self) -> bool:
        """房間夠安靜時換歌空檔改問一句記憶確認。真的問出口才回 True。"""
        try:
            mem = getattr(self.bot.router, "memory", None)
            if mem is None:
                return False
            present = [n for n in self.get_online_members() if not is_pseudo_player(n)]
            tastes: dict[str, dict] = {}
            taboos: dict[str, set] = {}
            for n in present:
                if mem.has_player(n):
                    pm = mem.get_player_memory(n)
                    tastes[n] = pm.get("taste", {})
                    taboos[n] = set(pm.get("taboos", []))
            picked = pick_question(
                present=present, tastes=tastes, taboos=taboos,
                artists=_load_artist_names(), log_rows=_load_memory_check_log(),
                now=time.time(), last_speech_ts=self.last_player_speech_time,
            )
            if picked is None:
                return False
            speaker, item = picked
            spoken = await self.play_tts(question_text(speaker, item), kind=SpeakKind.MEMORY_CHECK)
            if spoken is not True:
                return False
            _append_memory_check_log({"ts": time.time(), "event": "asked", "speaker": speaker, "item": item})
            self._memory_check_window = {"speaker": speaker, "item": item, "deadline": time.time() + ANSWER_WINDOW_S}
            asyncio.get_running_loop().call_later(ANSWER_WINDOW_S + 0.5, self._memory_check_expire, speaker, item)
            return True
        except Exception:
            logger.warning("🧠 [MemoryCheck] maybe_ask_memory_check 失敗", exc_info=True)
            return False

    def _memory_check_expire(self, speaker: str, item: str) -> None:
        window = getattr(self, "_memory_check_window", None)
        if window is not None and window.get("speaker") == speaker and window.get("item") == item:
            _append_memory_check_log({"ts": time.time(), "event": "answer", "speaker": speaker,
                                      "item": item, "answer": "none"})
            self._memory_check_window = None

    def _memory_check_intercept(self, speaker: str, raw_text: str, is_wake_check: bool) -> bool:
        """窗內本人第一句 → 吞掉解讀對/不對，回 True。其他情況回 False 照常往下走。"""
        if is_wake_check:
            return False
        window = getattr(self, "_memory_check_window", None)
        if not answer_window_open(window, speaker, time.time()):
            return False
        item = window["item"]
        ans = classify_answer(raw_text)
        self._memory_check_window = None
        _append_memory_check_log({"ts": time.time(), "event": "answer", "speaker": speaker,
                                  "item": item, "answer": ans})
        if ans == "unclear":
            return False
        asyncio.create_task(self._memory_check_apply(speaker, item, ans == "yes"))
        return True

    async def _memory_check_apply(self, speaker: str, item: str, confirmed: bool) -> None:
        try:
            mem = getattr(self.bot.router, "memory", None)
            if mem is None:
                return
            if confirmed:
                mem.record_taste_signal(speaker, item, CONFIRM_DELTA, reason="memory_check_confirm")
                await self.play_tts("好，記住了", kind=SpeakKind.RECALL_CONFIRM)
            else:
                mem.remove_taste_item(speaker, item)
                await self.play_tts("好，拿掉了", kind=SpeakKind.RECALL_CONFIRM)
        except Exception:
            logger.warning("🧠 [MemoryCheck] _memory_check_apply 失敗", exc_info=True)

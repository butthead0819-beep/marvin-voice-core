"""
WakeSessionMixin —— VoiceController 的喚醒後免喚醒回合對話接線。

從 voice_controller.py 抽出（棘輪只准降，見 tests/test_voice_controller_size_budget.py）。
self 仍是 VoiceController 實例，沿用原本 self 存取，核心狀態機邏輯都在 wake_session.py
（純函式、零 IO），這裡只負責 mixer/TTS/intent bus 的接線與提示音。

⚠️ 提示音（_wake_session_play_ack）絕對不准走 play_tts：play_tts 推送成功會呼叫
_wake_session_on_replied，提示音走 play_tts＝提示音觸發自己的安裝點（CLAUDE.md 明令
禁止的 callback 自觸發迴圈，見 2026-09-17 mixer 自癒事故）。改走 play_dj_on_tts_layer，
它不經 play_tts。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from tts_speak_policy import SpeakKind

logger = logging.getLogger(__name__)

WAKE_SESSION_ACK_PATH = "assets/wake_session_open.wav"
_ACK_POLL_S = 0.1
_ACK_IDLE_REQUIRED_S = 1.5   # mixer TTS 層連續空閒這麼久才算「回應播完」（跨過串流句與句的空檔）
_ACK_MAX_WAIT_S = 60.0
_ACK_SPEECH_WAIT_S = 15.0   # 等 Marvin 開口的上限；超過視為無語音回應，直接進入等空閒
_SPEAKING_EPS_S = 0.05
_WAKE_SESSION_REPLY_KINDS = frozenset({
    SpeakKind.WAKE_REPLY, SpeakKind.RESULT_NOTICE,
    SpeakKind.RECALL_CONFIRM, SpeakKind.LONG_READ,
})


def _is_music_action(winner) -> bool:
    """點播 / 下一首 / 重播被執行＝歌曲對話以指令收尾。"""
    if winner is None:
        return False
    name = getattr(winner, "name", "")
    if name == "music":
        return not getattr(winner, "missing_slots", None)   # 還在反問哪一首 → 對話繼續
    if name == "replay":
        return True
    if name == "playback_control":
        return str(getattr(winner, "reason", "")).startswith("skip:")
    return False


class WakeSessionMixin:
    def _marvin_tts_speaking(self) -> bool:
        mixer = getattr(self, "_mixer", None)
        return mixer is not None and mixer.tts_load_seconds() > _SPEAKING_EPS_S

    def _followup_override(self, speaker, fusion, text="") -> bool:
        """取代舊的 follow-up 判斷：WakeSession 對發起者免喚醒（優先）＋ 舊問句窗（D2-A 不變）。"""
        session = getattr(self, "_wake_session", None)
        if session is not None and session.on_utterance(
                speaker, time.time(), marvin_speaking=self._marvin_tts_speaking(), text=text):
            return True        # log 已在核心印
        if not self._wake_response_pending and fusion is not None and fusion.is_open():
            logger.info(f"🎧 [Follow-Up] {speaker} captured in follow-up window (reason={getattr(fusion, '_open_reason', '?')})")
            return True
        return False

    def _wake_session_on_accept(self, speaker, now) -> None:
        session = getattr(self, "_wake_session", None)
        if session is not None:
            session.on_wake_accepted(speaker, now)

    def _wake_session_on_ignored(self, speaker) -> None:
        session = getattr(self, "_wake_session", None)
        if session is not None:
            session.on_ignored(speaker, time.time())

    def _wake_session_on_shortcut(self, sc: str) -> None:
        from music_fastpath import FASTPATH_PLAY_PREFIX
        session = getattr(self, "_wake_session", None)
        if session is None:
            return
        if sc.startswith(FASTPATH_PLAY_PREFIX) or sc == "下一首":
            session.on_music_action(time.time())
            return
        self._wake_session_on_replied()

    def _wake_session_note_grounded(self, speaker) -> None:
        session = getattr(self, "_wake_session", None)
        if session is not None:
            session.note_grounded(speaker)

    def _wake_session_on_followup_result(self, speaker, hit: bool) -> None:
        session = getattr(self, "_wake_session", None)
        if session is not None:
            session.on_followup_result(speaker, hit)

    def _wake_session_grounded_sticky(self, speaker) -> bool:
        session = getattr(self, "_wake_session", None)
        return session is not None and session.wants_grounded_followup(speaker)

    def _wake_session_on_replied(self, winner=None) -> None:
        session = getattr(self, "_wake_session", None)
        if session is not None and _is_music_action(winner):
            session.on_music_action(time.time())
            return
        if session is None or not session.on_replied(time.time()):
            return
        if os.getenv("MARVIN_WAKE_SESSION_ACK", "1") == "0":
            return
        task = getattr(self, "_wake_session_ack_task", None)
        if task is not None and not task.done():
            return
        self._wake_session_ack_task = asyncio.create_task(self._wake_session_play_ack(session.target))

    async def _wake_session_play_ack(self, target) -> None:
        try:
            if getattr(self, "_mixer", None) is None:
                return
            speech_wait_s = 0.0
            while not self._marvin_tts_speaking():
                if speech_wait_s >= _ACK_SPEECH_WAIT_S:
                    break
                await asyncio.sleep(_ACK_POLL_S)
                speech_wait_s += _ACK_POLL_S
            idle_s = 0.0
            waited_s = 0.0
            while idle_s < _ACK_IDLE_REQUIRED_S:
                if waited_s >= _ACK_MAX_WAIT_S:
                    return
                await asyncio.sleep(_ACK_POLL_S)
                waited_s += _ACK_POLL_S
                if self._marvin_tts_speaking():
                    idle_s = 0.0
                else:
                    idle_s += _ACK_POLL_S
            session = getattr(self, "_wake_session", None)
            if session is None or session.target != target or not session.opened \
                    or session.awaiting_since is not None:
                return
            await self.play_dj_on_tts_layer(WAKE_SESSION_ACK_PATH, peak=0.15)
            logger.info(f"🔔 [WakeSession] 開窗提示音 target={target}")
        except Exception as e:
            logger.debug(f"[WakeSession] 提示音播放失敗（忽略）: {e}", exc_info=True)

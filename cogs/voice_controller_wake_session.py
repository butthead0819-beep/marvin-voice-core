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
import collections
import logging
import os
import random
import time
import weakref

from barge_in import (
    ACK_WAIT_MAX_S, BARGE_ACK_FILES, CONFIRM_TIMEOUT_S,
    clean_confirm, confirm_step, interrupted_text, user_done,
)
from reply_address import address_prefix
from tts_speak_policy import SpeakKind

logger = logging.getLogger(__name__)

WAKE_SESSION_ACK_PATH = "assets/wake_session_open.wav"
_ACK_POLL_S = 0.1
_ACK_IDLE_REQUIRED_S = 1.5   # mixer TTS 層連續空閒這麼久才算「回應播完」（跨過串流句與句的空檔）
_ACK_MAX_WAIT_S = 60.0
_ACK_SPEECH_WAIT_S = 15.0   # 等 Marvin 開口的上限；超過視為無語音回應，直接進入等空閒
_SPEAKING_EPS_S = 0.05
_BARGE_WATCH_POLL_S = 0.1

_BARGE_CONFIRM_SYSTEM = (
    "你是 Marvin。你剛才在回答使用者，講到一半被他打斷。用一句不超過20字的口語確認他現在"
    "真正想問什麼，例如『喔，你是要問XX』。不要回答問題、不要道歉、不要解釋。只輸出那一句；"
    "看不出他要問什麼就只輸出「略」。"
)
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
    def _init_barge_in_state(self) -> None:
        """✋ [BargeIn] 狀態初始化（VoiceController.__init__ 呼叫）。"""
        self._reply_lines = collections.deque(maxlen=8)   # Marvin 實際開口的句子（_stream_tts_to_mixer 記）
        self._reply_gen = 0                               # 被打斷 +1；舊世代串流的後續句子不念
        self._reply_stream_gens = weakref.WeakKeyDictionary()   # 串流 task → 開始時的世代號
        self._barge_watch_task = None

    def _maybe_start_barge_watch(self, speaker, user_id) -> None:
        """VAD speech-start 回呼進來：只有對談窗 target、Marvin 正在講 TTS（非 protected/shield）
        才建 watcher task。這裡不准直接切 TTS 或播聲音（回呼只標記，交給 task 處理）。"""
        from cogs.voice_controller_playback import barge_in_shielded   # 延遲 import：playback 反向 import 本模組
        session = getattr(self, "_wake_session", None)
        if (session is None or session.target != speaker or not session.opened
                or user_id is None
                or not self._marvin_tts_speaking()
                or self._tts_protected
                or barge_in_shielded(getattr(self, '_tts_barge_shield_until', 0.0), time.time())):
            return
        task = getattr(self, "_barge_watch_task", None)
        if task is None or task.done():
            self._barge_watch_task = asyncio.create_task(
                self._barge_in_watch(speaker, user_id, time.time()))

    def _marvin_tts_speaking(self) -> bool:
        mixer = getattr(self, "_mixer", None)
        return mixer is not None and mixer.tts_load_seconds() > _SPEAKING_EPS_S

    def _followup_override(self, speaker, fusion, text="", is_wake_check=False) -> bool:
        """取代舊的 follow-up 判斷：WakeSession 對發起者免喚醒（優先）＋ 舊問句窗（D2-A 不變）。"""
        session = getattr(self, "_wake_session", None)
        if session is not None and session.on_utterance(
                speaker, time.time(), marvin_speaking=self._marvin_tts_speaking(),
                text=text, final=not is_wake_check):
            if session.barge_captured:
                session.barge_captured = False
                asyncio.create_task(self._barge_in_confirm(speaker, text))
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

    def _wake_session_note_qa(self, speaker, question, answer) -> None:
        session = getattr(self, "_wake_session", None)
        if session is not None:
            session.note_qa_turn(speaker, question, answer)

    def _wake_session_qa_history(self, speaker) -> list:
        session = getattr(self, "_wake_session", None)
        return session.qa_history(speaker) if session is not None else []

    def _wake_session_grounded_sticky(self, speaker) -> bool:
        session = getattr(self, "_wake_session", None)
        return session is not None and session.wants_grounded_followup(speaker)

    def _wake_session_captured(self, speaker) -> bool:
        """session 已開窗且是 target 的句子（免喚醒接住的追問）→ 不播 filler ack（提示音已表示在聽）。"""
        session = getattr(self, "_wake_session", None)
        return session is not None and session.target == speaker and session.opened

    def _reply_address_prefix(self, speaker, text: str = "") -> str:
        try:
            members = self.get_online_members()
        except Exception:
            return ""
        return address_prefix(speaker, members, text)

    def _speak_reply_sentence(self, speaker, sentence: str, *, first: bool, emotion_tag: str = "neutral") -> None:
        """串流回覆的逐句發聲。stream_mode（放歌中）不逐句念——結尾整段 speak 會念（否則念兩次，10/17）。
        第一句多人在場前面加名字。"""
        if self.stream_mode:
            return
        gens = getattr(self, "_reply_stream_gens", None)
        task = asyncio.current_task()
        if gens is not None and task is not None:
            gen = getattr(self, "_reply_gen", 0)
            if first or task not in gens:
                gens[task] = gen
            elif gens[task] != gen:
                logger.info(f"✋ [BargeIn] {speaker} 舊回答串流已被打斷，不念: '{sentence[:20]}'")
                return
        prefix = self._reply_address_prefix(speaker, sentence) if first else ""
        asyncio.create_task(self.play_tts(prefix + sentence, already_in_channel=True, emotion_tag=emotion_tag))

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

    async def _barge_in_confirm(self, speaker, user_text) -> None:
        """target 打斷成功後，用一句話確認他真正想問什麼（同時 Marvin 準備新回答）。"""
        try:
            t0 = time.time()
            prev = interrupted_text(getattr(self, "_reply_lines", ()), t0)
            router = getattr(self, "_shared_tier_router", None)
            if router is None:
                return
            prompt = f"你剛才說（被打斷）：{prev or '（無）'}\n他打斷你說：{user_text}"
            try:
                raw = await asyncio.wait_for(
                    router.quick(prompt=prompt, caller="barge_in_confirm",
                                 system=_BARGE_CONFIRM_SYSTEM, max_tokens=60, temperature=0.3),
                    CONFIRM_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.info("⏭️ [BargeIn] 確認句逾時")
                return
            sent = clean_confirm(raw)
            if not sent:
                logger.info(f"⏭️ [BargeIn] 確認句無效 raw={raw!r}")
                return
            if any(ts > t0 for ts, _ in getattr(self, "_reply_lines", ())):
                logger.info("⏭️ [BargeIn] 主答案已開始，略過確認句")
                return
            self._tts_interrupted = False
            await self._stream_tts_to_mixer(sent, force_macos=False, emotion_tag="neutral", voice=None)
            logger.info(f"💬 [BargeIn] 確認句 {sent!r} latency={(time.time() - t0) * 1000:.0f}ms")
        except Exception as e:
            logger.debug(f"[BargeIn] 確認句處理失敗（忽略）: {e}", exc_info=True)

    async def _barge_in_watch(self, speaker, user_id, onset: float) -> None:
        """對談窗 target 持續講話超過 BARGE_CONFIRM_S → 真的切 TTS；短促出聲（dip）則放棄。"""
        try:
            while True:
                sink = self.bot.engine.get_active_sink()
                if sink is None:
                    return
                last = sink.user_last_spoken_time.get(user_id, 0.0)
                session = getattr(self, "_wake_session", None)
                if session is None or session.target != speaker or not session.opened:
                    return
                if not self._marvin_tts_speaking():
                    return
                step = confirm_step(onset, time.time(), last)
                if step == "dip":
                    logger.info(f"🤏 [BargeIn] {speaker} 短促出聲 {time.time() - onset:.1f}s，不切")
                    return
                if step == "cut":
                    break
                await asyncio.sleep(_BARGE_WATCH_POLL_S)

            if self._plan12 and self._mixer is not None:
                self._mixer.clear_tts()
            else:
                device = self._resolve_playback_device()
                if device is not None and device.is_playing():
                    device.stop()
            self._tts_interrupted = True
            self._reply_gen += 1
            ack_task = getattr(self, "_wake_session_ack_task", None)
            if ack_task is not None and not ack_task.done():
                ack_task.cancel()
            prev = interrupted_text(getattr(self, "_reply_lines", ()), time.time())
            session.mark_last_answer_interrupted(prev)
            session.note_barge_in(time.time())
            logger.info(f"✋ [BargeIn] {speaker} 打斷 Marvin（持續 {time.time() - onset:.1f}s）")
            self.stt_logger.info(f"[BOT被打斷←{speaker}] 未說完={prev[:80]}")

            waited = 0.0
            while True:
                sink = self.bot.engine.get_active_sink()
                last = sink.user_last_spoken_time.get(user_id, 0.0) if sink is not None else 0.0
                if user_done(time.time(), last):
                    break
                if waited >= ACK_WAIT_MAX_S:
                    return
                await asyncio.sleep(_BARGE_WATCH_POLL_S)
                waited += _BARGE_WATCH_POLL_S
            await self.play_dj_on_tts_layer(random.choice(BARGE_ACK_FILES))
            logger.info(f"🔔 [BargeIn] ack 已播 target={speaker}")
        except Exception as e:
            logger.debug(f"[BargeIn] watcher 失敗（忽略）: {e}", exc_info=True)

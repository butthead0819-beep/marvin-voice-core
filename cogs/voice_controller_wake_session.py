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
from helper_wake import helper_speak_plan
from intent_bus import WAKE_REQUIRED
from reply_address import address_prefix
from tts_speak_policy import SpeakKind
import turn_outcome

logger = logging.getLogger(__name__)

_STORM_WINDOW = 60.0   # 計數窗口（從 _apply_wake_guards 搬來，供 _record_accepted_wake 用）
_STORM_LIMIT = 4       # 窗口內喚醒次數門檻
_STORM_CLEAR_QUIET = 12.0   # 連續 12s 無新喚醒 → 風暴消散（_apply_wake_guards 也用這一份）

WAKE_SESSION_ACK_PATH = "assets/wake_session_open.wav"
_ACK_POLL_S = 0.1
_ACK_IDLE_REQUIRED_S = 1.5   # mixer TTS 層連續空閒這麼久才算「回應播完」（跨過串流句與句的空檔）
_ACK_MAX_WAIT_S = 60.0
_ACK_SPEECH_WAIT_S = 15.0   # 等 Marvin 開口的上限；超過視為無語音回應，直接進入等空閒
_SPEAKING_EPS_S = 0.05
_BARGE_WATCH_POLL_S = 0.1
_IBA_WAKE_VOICE_MIN = 0.9   # IBA voice 通道：≥ 這個才算真的叫了名字（一週實測有喚醒詞 0.95/1.0、沒有 0.3）

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
    """點播 / 播放控制 / 音量 / 重播被執行＝對話以指令收尾。

    10/23 實測：音量、繼續播放做完沒收窗，接走朋友互聊 2 分鐘；指令一律要喚醒詞，
    窗內不叫名字的「再大聲一點」本來就不該接。
    """
    if winner is None:
        return False
    name = getattr(winner, "name", "")
    if name == "music":
        return not getattr(winner, "missing_slots", None)   # 還在反問哪一首 → 對話繼續
    return name in ("replay", "playback_control", "volume")


class WakeSessionMixin:
    def _release_response_lock(self, reason: str) -> None:
        """回應處理結束 → 解除 Response Lock（1b790f9 刪掉 play_tts 結束解鎖後的補回）。"""
        if getattr(self, "_wake_response_pending", False):
            self._wake_response_pending = False
            logger.info(f"🔓 [Response Lock] 解鎖（{reason}）")

    async def _process_query_task_locked(self, task_data: dict) -> None:
        """佇列處理一筆喚醒查詢；不論成功、被丟棄或例外，結束時都解鎖。"""
        try:
            await turn_outcome.run_turn(
                self._process_query_task(task_data),
                speaker=task_data["speaker"], ts=task_data["timestamp"],
                discovered_by=task_data.get("discovered_by") or "wake_word")
        finally:
            self._release_response_lock("query_done")

    async def _release_lock_after(self, coro) -> None:
        """WakeShortcut 直派（不經佇列）：派發結束後解鎖。"""
        try:
            await coro
        finally:
            self._release_response_lock("shortcut_done")

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
        onset = time.time()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # prod：speech-start 回呼跑在 voice_recv PacketRouter 執行緒，不能在這裡 create_task
            self.bot.loop.call_soon_threadsafe(self._spawn_barge_watch, speaker, user_id, onset)
            return
        self._spawn_barge_watch(speaker, user_id, onset)

    def _spawn_barge_watch(self, speaker, user_id, onset: float) -> None:
        """在 event loop 上建 watcher task（同時只留一個）。"""
        task = getattr(self, "_barge_watch_task", None)
        if task is None or task.done():
            self._barge_watch_task = asyncio.create_task(
                self._barge_in_watch(speaker, user_id, onset))

    def _marvin_tts_speaking(self) -> bool:
        mixer = getattr(self, "_mixer", None)
        return mixer is not None and mixer.tts_load_seconds() > _SPEAKING_EPS_S

    def _followup_override(self, speaker, fusion, text="", is_wake_check=False) -> bool:
        """取代舊的 follow-up 判斷：WakeSession 對發起者免喚醒（優先）＋ 舊問句窗（D2-A 不變）。"""
        return self._followup_override_kind(speaker, fusion, text, is_wake_check=is_wake_check) is not None

    def _followup_override_kind(self, speaker, fusion, text="", is_wake_check=False) -> str | None:
        """同 _followup_override，但回傳命中的窗口種類（給 TurnOutcome 記錄用）。"""
        session = getattr(self, "_wake_session", None)
        if session is not None and session.on_utterance(
                speaker, time.time(), marvin_speaking=self._marvin_tts_speaking(),
                text=text, final=not is_wake_check):
            if session.barge_captured:
                session.barge_captured = False
                asyncio.create_task(self._barge_in_confirm(speaker, text))
            return "window_session"        # log 已在核心印
        if not self._wake_response_pending and fusion is not None and fusion.is_open():
            logger.info(f"🎧 [Follow-Up] {speaker} captured in follow-up window (reason={getattr(fusion, '_open_reason', '?')})")
            return "window_question"
        return None

    def _record_accepted_wake(self, speaker, segment_id, track, now) -> None:
        """[Wake Guards] 接受喚醒時的副作用（自 _apply_wake_guards 抽出，行為不變）。"""
        self.processed_wake_segments[segment_id] = track
        self._last_global_wake_time = now
        if not hasattr(self, "last_wake_time"): self.last_wake_time = {}
        self.last_wake_time[speaker] = now
        # 清理過期紀錄 (O(N) 雖然不完美但量少)
        if len(self.processed_wake_segments) > 100:
            self.processed_wake_segments = {k: v for k, v in list(self.processed_wake_segments.items())[-20:]}
        # 🔒 [Response Lock] 記錄本次喚醒被接受
        self._wake_response_pending = True
        self._wake_accepted_time = now
        self._wake_session_on_accept(speaker, now)
        # 🛡️ [Wake Storm Guard] 計數：滾動窗口超限 → 啟動動態風暴壓抑
        self._wake_burst_times.append(now)
        self._wake_burst_times = [t for t in self._wake_burst_times if now - t < _STORM_WINDOW]
        if len(self._wake_burst_times) >= _STORM_LIMIT:
            self._storm_active = True
            self._storm_last_wake_time = now
            self._wake_burst_times.clear()
            logger.warning(f"⚠️ [Wake Storm] {_STORM_WINDOW:.0f}s 內喚醒 {_STORM_LIMIT} 次，啟動風暴壓抑（{_STORM_CLEAR_QUIET:.0f}s 靜默後自動解除）")

    async def _etd_semantic_check(self, speaker, combined_text):
        """Semantic ETD Track B-2 的呼叫點（對談極速通道 F）。
        對談窗內：不問 Groq，回合成結果（已講完、文字不改），remember_etd 照存 → worker 必命中重用。
        MARVIN_FASTLANE_SKIP_CLEAN=0 時照舊呼叫 clean_stt_text。"""
        if os.environ.get("MARVIN_FASTLANE_SKIP_CLEAN", "1") != "0" and \
                self.in_conversation_window(speaker, time.time()):
            logger.info(f"⚡ [FastLane] {speaker} 對談窗內跳過 Groq ETD")
            return {"text": combined_text, "is_complete": True}
        return await self.bot.router.clean_stt_text(combined_text)

    def in_conversation_window(self, speaker, now) -> bool:
        """唯讀接點（意圖漏斗 eng review D3=C）：speaker 是否在對談窗內。

        對談極速通道判斷「是否在對談窗」只准用這個；VAD（voice_recv 執行緒）也會呼叫，
        所以只讀、不改狀態、不建 task。漏斗 3b 換成 ConversationState 時只改這裡的實作。"""
        session = getattr(self, "_wake_session", None)
        return session is not None and session.is_window_open(speaker, now)

    def _wake_session_on_accept(self, speaker, now) -> None:
        session = getattr(self, "_wake_session", None)
        if session is not None:
            session.on_wake_accepted(speaker, now)

    def _wake_session_on_ignored(self, speaker) -> None:
        turn_outcome.mark_route("dropped:quality_gate")
        session = getattr(self, "_wake_session", None)
        if session is not None:
            session.on_ignored(speaker, time.time())

    def _wake_session_on_shortcut(self, sc: str) -> None:
        session = getattr(self, "_wake_session", None)
        if session is None:
            return
        session.on_music_action(time.time())   # shortcut 只回完整指令（點播／控制）→ 一律收窗

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

    def _wakeless_in_window(self, speaker, text: str) -> bool:
        """窗內 target 的句子且沒叫名字 → 指令不執行（2026-10-24：可能只是在討論）。"""
        from wake_detector import has_mangled_wake
        t = (text or "").lower()
        return (self._wake_session_captured(speaker) and not any(w.lower() in t for w in self._WAKE_PATTERNS)
                and not has_mangled_wake(text))

    def _iba_wake_word_ok(self, speaker, raw_text, is_fast, ch) -> bool:
        """IBA 判喚醒但沒叫名字 → 不放行（2026-10-24：指令一律要喚醒詞）。

        一週實測：v≥0.95 全含喚醒詞、v=0.3 全沒有（誤觸發＋聽錯的喚醒詞）。
        N-best 救援（alt_wake）與聽錯版喚醒詞（has_mangled_wake）照放；ch=None＝legacy 路徑不動。
        """
        if not is_fast or ch is None:
            return is_fast
        from wake_detector import has_mangled_wake
        if (ch.get("voice") or 0) >= _IBA_WAKE_VOICE_MIN or (ch.get("alt_wake") or 0) > 0 or has_mangled_wake(raw_text):
            return True
        logger.info(f"🚫 [NoWakeWord] {speaker} IBA 判喚醒但沒叫名字（v={ch.get('voice')}），不放行：'{raw_text[:40]}'")
        return False

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

    def _speak_reply_sentence(self, speaker, sentence: str, *, first: bool, emotion_tag: str = "neutral",
                               music: bool | None = None) -> None:
        """串流回覆的逐句發聲。放歌中不逐句念——結尾整段 speak 會念（否則念兩次，10/17）。
        第一句多人在場前面加名字。

        music：回覆開始時的放歌狀態（呼叫端快照，10/23 review：避免逐句期間
        stream_mode 中途變化造成漏念/重念）；None＝讀當下 stream_mode。
        """
        if self.stream_mode if music is None else music:
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

    def _speak_whole_reply(self, speaker, full_text: str, *, music: bool, helper: bool,
                            suppressed: bool, emotion_tag: str = "neutral") -> None:
        # 🎚️ [Mid-song Answer] 音樂中不逐句念（_speak_reply_sentence 跳過）；收完整段後，
        # 若整段夠短（≤ STREAM_BUDGET，= B 給 LLM 的音樂字數預算）一次性走熱切換注入發聲，
        # 過長則維持靜音、只留貼文。短答案才聽得到，避免長答案佔用音樂太久。
        if music and not suppressed and full_text:
            asyncio.create_task(self.speak(self._reply_address_prefix(speaker, full_text) + full_text, emotion_tag=emotion_tag))

        # 🔍 [Helper Query] 非串流模式收完整段後：短答案整段念、長答案只念短通知
        # （完整內容已在文字貼文）。串流期間已 defer，這裡是唯一發聲點。
        if helper and full_text and not suppressed and not music:
            _mode, _say = helper_speak_plan(full_text, speaker)
            if _mode == "notify":
                logger.info(f"🔍 [Helper Query] {speaker} 長答案({len(full_text)}字)→貼文＋口播通知")
            # information 類：唸完不被使用者開口中斷（用戶政策 tier 1）
            asyncio.create_task(self.play_tts(self._reply_address_prefix(speaker, _say) + _say, already_in_channel=True, emotion_tag=emotion_tag, protected=True))

    def _wake_session_on_replied(self, winner=None) -> None:
        session = getattr(self, "_wake_session", None)
        if session is not None and getattr(winner, "name", None) == WAKE_REQUIRED:
            session.on_ignored(session.target, time.time())   # 窗內沒叫名字的指令：當沒對 Marvin 講
            return
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

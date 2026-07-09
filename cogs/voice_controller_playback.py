"""
PlaybackMixin — VoiceController 的 TTS 渲染 + Plan12 mixer 播放。

從 voice_controller.py 抽出（減肥），以 mixin 併入 VoiceController。self 仍是
VoiceController 實例，_mixer / stream_mode / _tts_protected / bot.voice_clients /
active_text_channel 等沿用原本 self 存取，行為零改動。外部呼叫者全是實例呼叫
（vc.play_tts / vc.speak / vc.play_dual_dialogue），方法仍在 VoiceController 上。

MAX_HOTSWAP_CHARS（play_tts 預設參數）定義在此，voice_controller re-export 給測試。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import shutil
import subprocess
import tempfile
import time

import numpy as np
import discord

from voice_guard_helpers import _should_mute_for_stream_guard
from utterance_budget import STREAM_BUDGET
from manzai_interject import compute_interject_ratio, interject_diagnostics
from local_mixing_source import (
    MixerPlaybackAdapter, S16ToF32MusicSource, BufferedF32MusicSource,
    ensure_mixer_playing, FRAME_BYTES_F32,
)

logger = logging.getLogger(__name__)

# play_tts 的 hotswap 預設字數上限（短 ack）。
MAX_HOTSWAP_CHARS = 12


class PlaybackMixin:
    def _ensure_mixer_playing(self, device) -> bool:
        """[Plan 12] flag=on 時確保 mixer adapter 正在 device 上播放（連線/重連後 re-arm）。

        每次交給 device.arm_mixer() 一個新 MixerPlaybackAdapter（不重用、reconnect-safe）。
        idempotent：已在播 → no-op。flag=off → 直接 no-op，不碰舊路徑。
        """
        if self._mixer is None:
            return False
        return ensure_mixer_playing(device, lambda: MixerPlaybackAdapter(self._mixer))

    async def _mixer_play_music(self, device, s16_source, *, still_active, volume_attr=None) -> None:
        """[Plan 12] 把 s16 音源餵 mixer 音樂層，等到播完 / 連線斷 / still_active() 變 False。

        volume_attr：要持續同步進 mixer 的 cog 音量屬性名（如 "stream_volume"）→ 語音/按鈕
        調音量 100ms 內即時生效（無 hotswap）。播完（來源耗盡 mixer 自清）或被中止即 return。
        """
        self._ensure_mixer_playing(device)
        # 背景預讀解耦 ffmpeg pipe（修 T5 串流斷續）：~1s buffer
        buffered = BufferedF32MusicSource(S16ToF32MusicSource(s16_source), buffer_frames=50)
        self._mixer.set_music_source(buffered)
        # 「音樂停了為何停」是靜默盲點（ffmpeg stderr→DEVNULL、音源耗盡無 log）＝device 上
        # 「~3s 就中斷、無錯誤日誌」難查的根因。此處是所有停止路徑的唯一出口→退出時一律記
        # 原因＋音源統計（produced 幀數/underruns/eof_reason），下次不必猜是音源死還是被中止。
        _t0 = time.monotonic()
        _reason = "source_exhausted"   # has_music() 變 False＝音源耗盡（正常或提早死）
        try:
            while self._mixer.has_music():
                if not still_active():
                    _reason = "still_active_false"   # stream_mode/radio_mode 被外部關掉
                    self._mixer.clear_music()
                    return
                if not device.is_connected():
                    _reason = "disconnected"         # 播放裝置斷線（本機喇叭恆連→不會走這）
                    self._mixer.clear_music()
                    return
                if volume_attr is not None:
                    # 每首響度正規化常數增益（背景量好才有；沒量好=1.0 raw）。乘在使用者音量上，
                    # 一首一個常數 → 不在歌內 pumping。
                    _ng = self._stream_norm_gain.get(self._current_stream_url, 1.0)
                    self._mixer.set_volume(getattr(self, volume_attr) * _ng)
                self._ensure_mixer_playing(device)  # on-demand：重連後 adapter 沒了 → 重 arm
                await asyncio.sleep(0.1)
        finally:
            _st = buffered.stats()
            logger.info(
                "🎵 [Plan12_Mixer] 音樂層結束 reason=%s elapsed=%.1fs produced=%d "
                "underruns=%d eof=%s(%s)",
                _reason, time.monotonic() - _t0, _st["produced"],
                _st["underruns"], _st["eof"], _st["eof_reason"] or "-",
            )

    async def _ffmpeg_to_f32(self, *, input_path: str | None = None,
                             input_bytes: bytes | None = None) -> "np.ndarray | None":
        """[Plan 12] 解碼音訊（檔案或 bytes）成 48k stereo f32 interleaved array。

        async subprocess（對齊 STT 規範，不用 subprocess.run）；失敗回 None 讓 caller 降級。
        """
        src = "pipe:0" if input_bytes is not None else (input_path or "")
        if not src:
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin", "-loglevel", "quiet",
                "-i", src, "-ac", "2", "-ar", "48000", "-f", "f32le", "pipe:1",
                stdin=asyncio.subprocess.PIPE if input_bytes is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate(input=input_bytes)
        except Exception:
            logger.exception("[Plan12_Mixer] ffmpeg f32 解碼失敗")
            return None
        if not out:
            return None
        return np.frombuffer(out, dtype=np.float32)

    # 對比舒緩 bucket（PROVISIONAL，可 live-tune）：激動使用者 → 讓 Marvin 更緩/軟以反差安撫。
    # 每個 bucket 含 volume 欄位（只有親密路徑用到；Discord 路徑永遠不讀這些值）。
    _INTIMATE_AGITATED: dict[str, str] = {"rate": "-30%", "pitch": "-18Hz", "volume": "-20%"}
    _INTIMATE_LOW: dict[str, str]      = {"rate": "-22%", "pitch": "-12Hz", "volume": "-12%"}
    _INTIMATE_CALM: dict[str, str]     = {"rate": "-28%", "pitch": "-22Hz", "volume": "-18%"}
    _INTIMATE_TTS_MAP: dict[str, dict[str, str]] = {
        # AGITATED：soothe with slow/steady/soft Marvin
        "excited": _INTIMATE_AGITATED, "impatient": _INTIMATE_AGITATED,
        "angry": _INTIMATE_AGITATED,   "frustrated": _INTIMATE_AGITATED,
        "sarcastic": _INTIMATE_AGITATED, "nemo": _INTIMATE_AGITATED,
        # LOW：warm-gentle
        "depressed": _INTIMATE_LOW, "sad": _INTIMATE_LOW, "hesitant": _INTIMATE_LOW,
        # CALM：gentle baseline（neutral, robotic, amused, marmo）
        "neutral": _INTIMATE_CALM, "robotic": _INTIMATE_CALM,
        "amused": _INTIMATE_CALM, "marmo": _INTIMATE_CALM,
    }

    def _current_softness(self) -> float:
        """安全讀取 meta_analyzer.last_softness；任何一層缺失都回 0.0，永不拋例外。

        只在親密分支呼叫——Discord（intimate OFF）路徑永遠不進來。
        """
        return getattr(
            getattr(getattr(getattr(self, "bot", None), "engine", None), "meta_analyzer", None),
            "last_softness", 0.0,
        )

    @staticmethod
    def _apply_softness_to_volume(vol: "str | None", softness: float) -> str:
        """依軟度調整 volume 字串（例如 '-18%'）並回傳新字串（不修改原值）。

        SOFT_MAX=15（PROVISIONAL，可 live-tune）：softness=1.0 最多再壓低 15%。
        夾持範圍 [-60, 0]（PROVISIONAL）。
        """
        base = int(vol.rstrip("%")) if vol else 0  # None / '' → 0
        # SOFT_MAX=15 PROVISIONAL
        final = base - round(softness * 15)
        # 夾持 [-60, 0] PROVISIONAL
        final = max(-60, min(0, final))
        return f"{final}%"

    def _resolve_tts_params(self, emotion_tag: str) -> dict[str, str]:
        """回傳當前有效 TTS 語調參數。

        親密模式開啟：依 emotion_tag bucket 回傳對比舒緩語調（含 softness 調整後的 volume）。
        未知/None tag → CALM 預設。回傳 copy，永不修改 _INTIMATE_* 常數。
        親密模式關閉（Discord 路徑，getattr default False）→ 與原 inline lookup 完全一致，
        不讀 meta_analyzer。
        """
        if getattr(self, "_intimate_mode", False):
            profile = self._INTIMATE_TTS_MAP.get(emotion_tag, self._INTIMATE_CALM)
            out = dict(profile)
            out["volume"] = self._apply_softness_to_volume(
                profile.get("volume"), self._current_softness()
            )
            return out
        return self._EMOTION_TTS_PARAMS.get(emotion_tag, self._EMOTION_TTS_PARAMS["neutral"])

    async def _stream_tts_to_mixer(self, text: str, *, force_macos: bool,
                                   emotion_tag: str, voice: str | None, layer: int = 1,
                                   on_first_frame=None) -> int:
        """[Plan 12] 邊收 edge-tts、邊 ffmpeg 解碼、邊逐幀 push 進 TTS 層。

        layer=1：主 TTS 層（push_tts）；layer=2：打岔層（push_tts2，與 layer1 並行混音，
        漫才 Marmo 疊進來打斷 Marvin 用）。

        首音 ~0.8s 就出（不必等整段 render；恢復舊 FIFO streaming 的低延遲），且 render 全在
        event loop（非 voice thread）→ 不阻塞混音。回傳 push 進去的幀數。
        edge-tts chunks → ffmpeg stdin；ffmpeg f32le stdout → readexactly(一幀) → push_tts。
        """
        tp = self._resolve_tts_params(emotion_tag)
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin", "-loglevel", "quiet", "-i", "pipe:0",
                "-ac", "2", "-ar", "48000", "-f", "f32le", "pipe:1",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception:
            logger.exception("[Plan12_Mixer] TTS streaming ffmpeg 啟動失敗")
            return 0

        async def _feed():
            try:
                async for c in self.bot.tts_engine.stream_audio(
                    text, voice=voice, rate=tp["rate"], pitch=tp["pitch"],
                    volume=tp.get("volume"), force_macos=force_macos,
                ):
                    if c:
                        proc.stdin.write(c)
                        await proc.stdin.drain()
            except Exception:
                logger.warning("[Plan12_Mixer] edge-tts → ffmpeg 餵入中斷")
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        _push = self._mixer.push_tts2 if layer == 2 else self._mixer.push_tts

        async def _drain() -> int:
            pushed = 0
            while True:
                if self._tts_interrupted:  # 使用者打斷 → 立即停止餵（佇列已被 clear_tts 清掉）
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    break
                try:
                    data = await proc.stdout.readexactly(FRAME_BYTES_F32)
                except asyncio.IncompleteReadError as e:
                    if e.partial:
                        _push(np.frombuffer(e.partial, dtype=np.float32))
                        pushed += 1
                    break
                except Exception:
                    break
                _push(np.frombuffer(data, dtype=np.float32))
                pushed += 1
                if pushed == 1 and on_first_frame is not None:
                    try:
                        on_first_frame()
                    except Exception:
                        pass
            return pushed

        _, pushed = await asyncio.gather(_feed(), _drain())
        if pushed == 0:
            logger.warning(f"[Plan12_Mixer] TTS 推送 0 frame（text_len={len(text)}）— edge-tts 空流或 _tts_interrupted 被提早設起")
        return pushed

    async def speak(
        self,
        text: str,
        *,
        proactive: bool = False,
        max_chars: int = STREAM_BUDGET,
        already_in_channel: bool = True,
        emotion_tag: str = "neutral",
        protected: bool = False,
    ) -> None:
        """統一的 stream-aware TTS 入口（給 agent handler 用）。

        封裝 hotswap 接線 + proactive/response 差別，呼叫端不用記 play_tts 的
        6 個 kwargs 組合。新 agent 要說話呼叫這個，play_tts 留給內部 / 特殊
        case（force_macos / priority / voice 等）。

        proactive=False（預設，喚醒回應 / 對話）：
          - 非 stream → 正常播
          - stream → hotswap 注入（短的成功；超字按 play_tts line 5544 處理）

        proactive=True（greeting/farewell/idle/ack 等主動發話）：
          - 非 stream → 正常播
          - stream + ≤max_chars → hotswap 注入
          - stream + 超字 → 靜音貼文（fallback；silent_during_stream 行為）
          - 🎭 Marmo Case B：可能升級為 dual（Marvin → Marmo），機率閘
            MARMO_DUAL_CHANCE (default 0.5) + MARMO_DUAL_SPEAK 必須 on。
            失敗 fallback 走原 single Marvin 路徑。
        """
        # 🎭 [Marmo Case B] 機率升級為 dual (Marvin → Marmo)。
        # 只在 proactive=True 試（主動發話）；protected（如 join 招呼要唸完點名）不升級，
        # 確保是乾淨單句、不被 dual 機率閘洗掉名字/保護。
        if proactive and not protected and self._maybe_try_dual_upgrade():
            try:
                segments = await self._generate_dual_marvin_lead(text)
                if segments:
                    await self.play_dual_dialogue(segments)
                    return
            except Exception as exc:
                logger.warning(f"[Speak] dual upgrade failed, fallback single: {exc}")

        await self.play_tts(
            text,
            already_in_channel=already_in_channel,
            silent_during_stream=proactive,
            allow_hotswap=True,
            hotswap_max_chars=max_chars,
            emotion_tag=emotion_tag,
            protected=protected,
        )

    def _maybe_try_dual_upgrade(self) -> bool:
        """Roll the dice：MARMO_DUAL_SPEAK on + 隨機 < MARMO_DUAL_CHANCE + router 可用。

        每次呼叫現讀 env（hot-flippable，不必重啟）。
        """
        import random as _random
        if os.getenv("MARMO_DUAL_SPEAK", "").strip().lower() not in ("1", "true", "yes"):
            return False
        try:
            chance = float(os.getenv("MARMO_DUAL_CHANCE", "0.5"))
        except (TypeError, ValueError):
            chance = 0.5
        if _random.random() >= chance:
            return False
        if getattr(self.bot, "router", None) is None:
            return False
        return True

    async def _generate_dual_marvin_lead(self, text: str):
        """呼叫 dual generation service with pattern="marvin_lead"。"""
        from services.dialogue_generation import (
            generate_dual_dialogue,
            make_gemini_dual_dialogue_llm_fn,
        )
        llm_fn = make_gemini_dual_dialogue_llm_fn(self.bot.router)
        return await generate_dual_dialogue(
            content_text=text,
            llm_fn=llm_fn,
            pattern="marvin_lead",
        )

    async def play_tts(self, text: str, force_macos: bool = False, already_in_channel: bool = False, silent_during_stream: bool = False, emotion_tag: str = "neutral", voice: str = None, priority: int = 1, allow_hotswap: bool = False, hotswap_max_chars: int = MAX_HOTSWAP_CHARS, protected: bool = False):
        """
        🚀 [T-02 Opt] Hyper-Streaming Version (Plan 12 Simplified)
        """
        if self.game_mode and not self._tts_protected:
            return  # 遊戲中停止所有 TTS
        if not text: return
        import re
        text = re.sub(r'<think(?:ing)?>.*?</think(?:ing)?>', '', text, flags=re.DOTALL).strip()
        if not text: return

        # 🎵 [Stream Guard]
        if _should_mute_for_stream_guard(self.stream_mode, silent_during_stream, allow_hotswap):
            return

        # 🦆 [Hot-Chat Guard]
        if silent_during_stream and self._room_mood_store.get(0).hot_chat:
            logger.info(f"🦆 [Hot-Chat Mute] 熱聊中靜音主動 TTS: '{text[:30]}'")
            return

        # 🛡️ [Interrupt Guard]
        if already_in_channel and self._tts_interrupted:
            logger.info(f"⏩ [TTS Interrupt Guard] 中斷後跳過剩餘片段: '{text[:25]}...'")
            return

        if not self._tts_protected:
            if not await self._wait_for_user_silence():
                logger.info(f"⏸️ [TTS Silence Gate] 使用者仍在說話，跳過非保護 TTS: '{text[:25]}...'")
                return

        # ⚠️ [Companion Radar]
        if os.getenv("COMPANION_RADAR_ENABLED", "false").lower() == "true":
            bridge = getattr(self.bot, "companion_bridge", None)
            if bridge is not None and getattr(bridge, "is_connected", False):
                try:
                    from marvin_voice_core.companion_radar import classify_risk
                    _atm_tracker = getattr(getattr(self.bot, "router", None), "atmosphere_tracker", None)
                    _atm_snap = None
                    if _atm_tracker is not None:
                        try:
                            _s = _atm_tracker.get_snapshot()
                            _atm_snap = {
                                "room_mood": getattr(_s, "room_mood", ""),
                                "dominant_topic": getattr(_s, "dominant_topic", ""),
                            }
                        except Exception:
                            _atm_snap = None
                    context = {"atmosphere_snapshot": _atm_snap}
                    risk = classify_risk(text, context)
                    if risk is not None:
                        approved = await bridge.request_radar_veto(
                            text, {"risk": risk}, timeout=2.0
                        )
                        if not approved:
                            logger.info(
                                f"[Companion_Radar] TTS vetoed by user: {text[:60]!r} (rule={risk.get('rule')})"
                            )
                            return
                except Exception as e:
                    logger.warning(f"[Companion_Radar] check failed (proceeding with TTS): {e}")

        # 🎛️ [Plan 12] render → push mixer
        device = self._resolve_playback_device()
        if device is None:
            logger.warning(f"⚠️ [TTS] 無可用播放裝置（_resolve_playback_device→None，local_mode={getattr(self, '_local_mode', False)}），丟棄本句: '{text[:30]}'")
            return
        if not already_in_channel:
            self._tts_interrupted = False
        _drop = {0: float("inf"), 1: 8.0, 2: 3.0}.get(priority, 8.0)
        _load = self._mixer.tts_load_seconds()
        if _load > _drop and not self._tts_protected:
            logger.info(f"⏭️ [TTS Load Drop] mixer TTS 佇列 {_load:.1f}s > {_drop}s（priority={priority}），丟棄本句: '{text[:30]}'")
            if not already_in_channel and self.active_text_channel:
                asyncio.create_task(self.active_text_channel.send(f"💬 {text}"))
            return
        self._ensure_mixer_playing(device)
        pushed = await self._stream_tts_to_mixer(text, force_macos=force_macos,
                                                 emotion_tag=emotion_tag, voice=voice)
        # [Follow-Up] D8: only open window when TTS was actually heard by users
        try:
            pushed_ok = bool(pushed > 0)
        except TypeError:
            pushed_ok = True
        if pushed_ok and os.getenv("MARVIN_FOLLOWUP_ENABLED", "true").lower() == "true":
            from wake_detector import _has_question_marker
            if _has_question_marker(text):
                _bridge = getattr(self.bot, "companion_bridge", None)
                _suppressed = _bridge is not None and getattr(_bridge, "_mode", None) in {"silent_5min", "shutup"}
                if not self.game_mode and not _suppressed:
                    _wd = getattr(getattr(self.bot, "router", None), "wake_fusion", None)
                    if _wd is not None:
                        _window = float(os.getenv("MARVIN_FOLLOWUP_WINDOW_SEC", "8.0"))
                        _wd.temporary_open_window(_window, reason="followup")

    async def _play_dual_interject(self, segments, *, duck=None, step=None, at=None) -> bool:
        """🎭 [打岔] Plan12 mixer 雙層疊播：Marvin 在 layer1，Marmo 在 Marvin 尾段(~80%)
        疊進 layer2 混音打斷。需 Plan12 mixer。成功回 True；前置不符/失敗回 False 讓
        caller 落序列 fallback。Marmo 疊進時 mixer 把 Marvin 逐漸 fade 到 _interject_duck。
        duck/step：taste-tuning 即時覆寫（webhook 帶 → 免重啟調 fade 終點/速度）。"""
        device = self._resolve_playback_device()
        if device is None or self._mixer is None:
            return False
        if duck is not None or step is not None:
            self._mixer.set_interject_params(duck=duck, step=step)
        marvin_seg = next((s for s in segments if s.get("voice") != "marmo"), None)
        marmo_seg = next((s for s in segments if s.get("voice") == "marmo"), None)
        marvin_text = (marvin_seg or {}).get("text", "").strip()
        marmo_text = (marmo_seg or {}).get("text", "").strip()
        if not marvin_text or not marmo_text:
            return False

        marmo_voice = os.getenv("MARMO_VOICE", "zh-TW-HsiaoYuNeural")
        self._tts_interrupted = False
        # 🛡️ 漫才是「演出」，整段唸完不該被一句話/咳嗽 barge-in 中斷（否則 _stream_tts_to_mixer
        # 的串流被 kill → 餵入中斷、沒聲音）。_tts_protected=True 讓 barge-in(2480) 略過。
        _prev_protected = self._tts_protected
        _armed = self._ensure_mixer_playing(device)
        self.is_playing_audio = True
        self._tts_protected = True
        _m1 = _m2 = 0
        try:
            dur = self.bot.tts_engine.get_estimated_duration(marvin_text)
            # at 沒手動傳 → 動態算（落 Marvin 子句中段、避開標點，不論對白長度都通用）
            _at = at if at is not None else compute_interject_ratio(marvin_text)
            marvin_task = asyncio.create_task(self._stream_tts_to_mixer(
                marvin_text, force_macos=False, emotion_tag="neutral", voice=None, layer=1))
            # 在 Marvin _at 比例處讓 Marmo 疊進 layer2 打斷（切句中、非標點處才像真打斷）。
            # 串流期間持續 re-arm adapter（on-demand idle 掉就重 arm，仿 _mixer_play_music）。
            _t_end = asyncio.get_event_loop().time() + max(0.5, dur * _at)
            while asyncio.get_event_loop().time() < _t_end:
                self._ensure_mixer_playing(device)
                await asyncio.sleep(0.1)
            # 量測 Marmo 首塊延遲：task 啟動 → 第一幀真正 push 進 mixer 的耗時
            # （耳朵聽到 Marmo 的時點 = 啟動時點 + 此延遲，是切入比例偏離設計的主因）。
            _marmo_t0 = asyncio.get_event_loop().time()
            _marmo_first = {"t": None}
            def _on_marmo_first():
                if _marmo_first["t"] is None:
                    _marmo_first["t"] = asyncio.get_event_loop().time()
            marmo_task = asyncio.create_task(self._stream_tts_to_mixer(
                marmo_text, force_macos=False, emotion_tag="marmo", voice=marmo_voice, layer=2,
                on_first_frame=_on_marmo_first))
            # 等兩路播完，期間持續 re-arm
            while not (marvin_task.done() and marmo_task.done()):
                self._ensure_mixer_playing(device)
                await asyncio.sleep(0.1)
            _m1, _m2 = marvin_task.result(), marmo_task.result()
        finally:
            self.is_playing_audio = False
            self._tts_protected = _prev_protected
        _marmo_lat = (_marmo_first["t"] - _marmo_t0) if _marmo_first["t"] is not None else 0.0
        _diag = interject_diagnostics(
            at_ratio=_at, est_dur_s=dur,
            marvin_frames=_m1, marmo_frames=_m2, marmo_first_chunk_s=_marmo_lat)
        logger.info(
            f"🎭 [DualInterject] 完成 armed={_armed} "
            f"marvin={_m1}幀({_diag['marvin_actual_s']:.1f}s/{len(marvin_text)}字) "
            f"marmo={_m2}幀({_diag['marmo_actual_s']:.1f}s/{len(marmo_text)}字) | "
            f"設計at={_at:.2f} est_dur={dur:.1f}s 觸發@{_diag['trigger_s']:.1f}s "
            f"marmo首塊+{_marmo_lat:.2f}s → 實際切入@{_diag['perceived_entry_s']:.1f}s "
            f"={_diag['perceived_ratio']:.0%} 重疊{_diag['overlap_s']:.1f}s")
        return True

    async def play_dual_dialogue(self, segments, *, interject: bool = False, duck=None, step=None, at=None):
        """🎭 [Marmo 一搭一唱] 雙段對白播放：[marvin, marmo] 按順序。

        interject=True 且 Plan12 mixer 可用 + 剛好兩段 → 走打岔疊播（Marmo 在 Marvin
        尾段混音進來）；前置不符或失敗 → 落下方序列播。

        segments: list[dict]，每個 {"voice": "marvin"|"marmo", "text": "..."}。
        順序強制 marvin → marmo 由 services/dialogue_generation.py 確保，
        此處只負責照 list 順序播。

        Lock 行為：每段 play_tts 各自 acquire/release playback_lock
        （asyncio.Lock 不可重入，外層不能再包 lock）。段間有 ~ms race window
        可能被音樂插入——PoC 接受；Phase 2 視需要再做 single-lock 重寫。

        失敗處理：play_tts 拋例外（例如 voice client disconnect） → bail，
        不繼續播下一段（避免半個 dual 造成詭異「Marvin 自言自語問空氣」）。
        """
        if not segments:
            return

        # 🎭 打岔模式（Plan12 mixer 雙層疊播）；前置不符/失敗 → 落下方序列
        if interject and self._plan12 and self._mixer is not None and len(segments) == 2:
            try:
                if await self._play_dual_interject(segments, duck=duck, step=step, at=at):
                    return
            except Exception as exc:
                logger.warning(f"🎭 [DualInterject] 失敗，落序列播: {exc}")

        # 🛡️ Reset interrupt guard：dual_speak 是 marmo_server 注入的獨立完整 unit，
        # 不是上次 wake reply 串流的續句；若上次 wake 被插話設了 _tts_interrupted=True
        # 殘留，會把整個 dual 兩段都跳過（PoC 6/1 實測到）。Reset 後正常播。
        self._tts_interrupted = False

        marmo_voice = os.getenv("MARMO_VOICE", "zh-TW-HsiaoYuNeural")

        for i, seg in enumerate(segments):
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            is_marmo = seg.get("voice") == "marmo"
            voice_arg = marmo_voice if is_marmo else None
            emotion_tag = "marmo" if is_marmo else "neutral"
            try:
                await self.play_tts(
                    text,
                    already_in_channel=True,
                    protected=True,  # 漫才演出唸完不中斷，不被靜音閘/barge-in 跳過
                    voice=voice_arg,
                    emotion_tag=emotion_tag,
                )
            except Exception as exc:
                logger.warning(f"🎭 [DualDialogue] play_tts 失敗 ({seg.get('voice')}): {exc}")
                return  # 段間 bail：避免半個 dual

            # 段間短停頓（不在最後一段）
            if i < len(segments) - 1:
                await asyncio.sleep(0.3)

    async def tts_flush(self):
        """
        🗑️ [Flush Policy] 強制中斷目前播放的 TTS、並清空所有 pending 語音隊列。
        """
        self._tts_flush_requested = True
        voice_client = next((vc for vc in self.bot.voice_clients if vc.is_connected()), None)
        if voice_client and voice_client.is_playing():
            voice_client.stop()
        self.tts_queue_duration = 0.0
        await asyncio.sleep(0.3)  # 讓在途 tasks 有機會通過 Flush Gate
        self._tts_flush_requested = False
        logger.info("🗑️ [TTS Flush] 佇列已清空，恢復正常播放。")

    async def play_local_file(self, file_path: str):
        """
        🚀 [Operation Broadcast] 播放本地音訊檔案。
        """
        if not os.path.exists(file_path):
            logger.warning(f"⚠️ [Local Play] 找不到檔案: {file_path}")
            return

        device = self._resolve_playback_device()
        if device is None:
            return

        self._mixer.set_volume(1.0)
        src = discord.FFmpegPCMAudio(file_path)
        await self._mixer_play_music(device, src, still_active=lambda: device.is_connected())

    def _cleanup_fifo(self, path, tmp_dir):
        """[Operation Cleanup] 安全移除命名管道與暫存目錄"""
        try:
            if os.path.exists(path): os.remove(path)
            if tmp_dir and os.path.exists(tmp_dir):
                if "tmp" in tmp_dir or "temp" in tmp_dir:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception as e:
            logger.debug(f"Cleanup warning: {e}")

    async def _release_queue_duration(self, duration: float):
        """🛡️ [T-02 Helper] 扣除 TTS 隊列預估時長"""
        self.tts_queue_duration = max(0.0, self.tts_queue_duration - duration)


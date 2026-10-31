"""puck 全時收音會話狀態機 — WebSocket 收 ADPCM，狀態機跑完整喚醒→收音→確認→回覆流程。

狀態：listen（平時聽候選喚醒）→ capture（候選喚醒後收指令原音）→
confirm（送 STT 二次確認是否真的喚醒）→ reply（已送出指令，等/壓低音量直到
Marvin 回完話或逾時）→ 回 listen。

跟 Discord 語音路徑共用 WakeStreamDetector（聲學層候選喚醒）+ PuckVADStream
（連續音訊切句）+ SileroStreamVAD；喚醒詞比對改用拼音版（puck_wake.py）。
"""
from __future__ import annotations

import asyncio
import io
import logging
import math
import os
import time
import wave
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from ima_adpcm import BLOCK_BYTES, BLOCK_SAMPLES, decode_block
from marvin_voice_core.icy_meta import SpeakTracker
from puck_vad_stream import PuckVADStream
from puck_wake import pinyin_wake_match, strip_through_wake
from silero_stream_vad import SileroStreamVAD
from swift_wake_stream import SwiftWakeStreamDetector
from wake_stream_detector import WakeStreamDetector

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 16000


def _puck_wake_engine() -> str:
    """車 HTTP 層（car_http_app.py 的 handle_mic_ws）跟 PuckMicSession 共用同一個
    判斷，決定要不要擋「whisper_model is None → 503」。"""
    # 2026-10-31 預設 swift：重播對比 mix 3/3 vs Whisper 2/3、CPU ~2% vs 27–40%
    engine = os.environ.get("MARVIN_PUCK_WAKE_ENGINE", "swift").strip().lower()
    if engine not in ("whisper", "swift"):
        logger.warning(f"🎙️ [PuckMic] 未知 MARVIN_PUCK_WAKE_ENGINE={engine!r}，當作 swift")
        return "swift"
    return engine


@dataclass
class PuckMicConfig:
    # 候選喚醒時，往前取這麼多秒原始音訊當指令開頭。Swift 串流喚醒從喚醒詞說完到 fire 要 1.5–2.5s
    # （音訊時間），1.5s 常把喚醒詞本身切掉 → confirm 否決；傾印重播 1.5s 8/16、4.0s 16/16（2026-10-31）
    capture_pre_s: float = 4.0
    capture_max_s: float = 6.0      # 候選喚醒後最多再收這麼多秒（音樂中 VAD 常不會停）
    speak_hold_s: float = 3.5       # SpeakTracker hold：0.4s ICY + iPhone 播放延遲 ~3s
    reply_start_timeout_s: float = 12.0  # 送出指令後這麼久 Marvin 都沒開口 → unduck
    unduck_idle_s: float = 3.0      # Marvin 開口後，安靜這麼久 → unduck
    unduck_cap_s: float = 30.0      # 送出指令後最長壓低時間
    stats_interval_s: float = 60.0
    ring_s: float = 4.5             # 原始 PCM ring buffer 長度（要 >= capture_pre_s）
    ptt_pre_s: float = 0.3          # PTT 強制喚醒：按下前只取這麼多（按了才開口，前面多半是音樂）
    wake_pause_wait_s: float = 5.0  # 只喊了喚醒詞（停下來等 ack）→ 最多等這麼久讓使用者開口
    face_speak_delay_s: float = 2.5  # 臉部「speak」狀態延後送：iPhone 播放延遲，車上實測再調


def _build_wav(pcm_bytes: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_SAMPLE_RATE)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


class PuckMicSession:
    def __init__(self, *, device: str, speaker: str, whisper_model, loop,
                 send_json: Callable[[dict], Awaitable[None]],
                 confirm_stt: Callable[[bytes], Awaitable[str]],
                 deliver: Callable[[str, str], Awaitable[None]],
                 tts_frames_total: Callable[[], int],
                 clock=time.monotonic,
                 vad=None,
                 config: Optional[PuckMicConfig] = None,
                 wake_engine: Optional[str] = None):
        self.device = device
        self.speaker = speaker
        self.send_json = send_json
        self.confirm_stt = confirm_stt
        self.deliver = deliver
        self.tts_frames_total = tts_frames_total
        self.clock = clock
        self.config = config or PuckMicConfig()
        self._loop = loop

        self._wake_engine = wake_engine if wake_engine is not None else _puck_wake_engine()
        if self._wake_engine not in ("whisper", "swift"):
            logger.warning(f"🎙️ [PuckMic] 未知 wake_engine={self._wake_engine!r}，當作 swift")
            self._wake_engine = "swift"

        if self._wake_engine == "swift":
            self._wake = SwiftWakeStreamDetector(
                self._on_wake, loop,
                wake_matcher=pinyin_wake_match,
                log_text=False,
            )
            # 全時餵（不經 VAD）：CPU ~2%（推論不在 CPU 上），且 VAD 段落開頭的喚醒詞
            # 不會被切掉（2026-10-31 mix.wav：VAD 閘控 1/3、全時 3/3）。只 arm 一次。
            self._wake.on_speech_start(0, 0.0)
        else:
            self._wake = WakeStreamDetector(
                whisper_model, self._on_wake, loop,
                input_rate=_SAMPLE_RATE, input_channels=1,
                initial_prompt=None, temperature=0.0,
                window_s=1.0, stride_s=0.25,
                wake_matcher=lambda t: pinyin_wake_match(t) is not None,
                contiguous=True,
                log_text=False,
            )
            orig_infer = self._wake._infer

            async def _counted_infer(*a, **kw):
                self._infer_count += 1
                t0 = self.clock()
                await orig_infer(*a, **kw)
                self._infer_elapsed_total += self.clock() - t0

            self._wake._infer = _counted_infer

        self._vad_stream = PuckVADStream(
            vad if vad is not None else SileroStreamVAD(),
            on_speech_start=self._vad_on_speech_start,
            on_pcm=self._vad_on_pcm,
            on_speech_end=self._vad_on_speech_end,
        )

        self._speak = SpeakTracker(hold_s=self.config.speak_hold_s)

        ring_maxlen = math.ceil(self.config.ring_s * _SAMPLE_RATE / BLOCK_SAMPLES)
        self._ring: deque[bytes] = deque(maxlen=ring_maxlen)

        self._state = "listen"
        self._samples_fed = 0
        self._capture_buf: list[bytes] = []
        self._capture_deadline = 0.0

        self._ducked = False
        self._confirm_task: Optional[asyncio.Task] = None

        self._delivered_at = 0.0
        self._reply_spoke = False
        self._last_speaking_at: Optional[float] = None

        self._speak_since: Optional[float] = None
        self._face_speaking = False
        self._face_sent: Optional[str] = None

        self._suppressed = False
        self._bad_packets = 0
        # 除錯用：MARVIN_PUCK_DUMP_DIR 有設就把收到的 16k PCM 原樣存成 WAV（每條連線一檔），離線重播比對
        self._dump_dir = os.environ.get("MARVIN_PUCK_DUMP_DIR", "").strip()
        self._dump: Optional[wave.Wave_write] = None

        self._candidates = 0
        self._confirmed = 0
        self._rejected = 0
        self._ptt = 0
        self._require_wake = True     # confirm 時 STT 文字必須含喚醒詞（PTT button 例外）
        # 收音要等「新的一段話」開始才准 speech_end 收尾（PTT、或喚醒詞後停頓等 ack）
        self._need_new_segment = False
        self._saw_new_segment = False
        self._wait_deadline = 0.0
        self._infer_count = 0
        self._infer_elapsed_total = 0.0

        self._last_stats_at = self.clock()

    @property
    def state(self) -> str:
        return self._state

    @property
    def stats(self) -> dict:
        vs = self._vad_stream.stats
        duty = vs["speech_windows"] / vs["windows"] if vs["windows"] else 0.0
        if self._wake_engine == "swift":
            infers = self._wake.stats["results"]
            mean_infer_ms = 0.0
        else:
            infers = self._infer_count
            mean_infer_ms = (self._infer_elapsed_total / self._infer_count * 1000) if self._infer_count else 0.0
        return {
            "windows": vs["windows"],
            "speech_windows": vs["speech_windows"],
            "duty": duty,
            "infers": infers,
            "mean_infer_ms": mean_infer_ms,
            "candidates": self._candidates,
            "confirmed": self._confirmed,
            "rejected": self._rejected,
            "ptt": self._ptt,
        }

    # ── 進音訊 ──────────────────────────────────────────────────────────────

    async def feed_adpcm(self, data: bytes) -> None:
        if len(data) % BLOCK_BYTES != 0:
            # 高頻路徑（每秒 ~4 包）：只記第 1 次與之後每 50 次，避免壞串流灌爆 log
            self._bad_packets += 1
            if self._bad_packets % 50 == 1:
                logger.warning(
                    f"🎙️ [PuckMic] {self.device} 收到非 {BLOCK_BYTES} 倍數長度（{len(data)}），"
                    f"丟棄整包（累計 {self._bad_packets}）")
            return

        n_blocks = len(data) // BLOCK_BYTES
        for i in range(n_blocks):
            block = data[i * BLOCK_BYTES:(i + 1) * BLOCK_BYTES]
            pcm = decode_block(block)
            pcm_bytes = pcm.tobytes()

            self._ring.append(pcm_bytes)
            if self._dump_dir:
                self._dump_write(pcm_bytes)
            if self._wake_engine == "swift" and not self._suppressed:
                self._wake.push_pcm(0, pcm_bytes)
            if self._state == "capture":
                self._capture_buf.append(pcm_bytes)

            self._samples_fed += BLOCK_SAMPLES
            self._vad_stream.feed(pcm)

            if self._state == "capture" and self._samples_fed >= self._capture_deadline:
                self._finish_capture("max")
            elif (self._state == "capture" and self._need_new_segment and not self._saw_new_segment
                    and self._samples_fed >= self._wait_deadline):
                self._finish_capture("no_cmd")

        await self._post_block_checks()

    async def _post_block_checks(self) -> None:
        now = self.clock()
        speaking = self._speak.update(self.tts_frames_total(), now)

        if speaking and self._speak_since is None:
            self._speak_since = now
        elif not speaking:
            self._speak_since = None
        self._face_speaking = (
            speaking and self._speak_since is not None
            and (now - self._speak_since) >= self.config.face_speak_delay_s)

        want_suppressed = speaking and self._state == "listen"
        if want_suppressed != self._suppressed:
            self._vad_stream.set_suppressed(want_suppressed)
            self._suppressed = want_suppressed
            logger.info(f"🎙️ [PuckMic] {self.device} {'Marvin 講話，暫停喚醒' if want_suppressed else '恢復喚醒'}")

        if self._state == "reply":
            cfg = self.config
            if speaking:
                self._reply_spoke = True
                self._last_speaking_at = now

            if (self._reply_spoke and self._last_speaking_at is not None
                    and (now - self._last_speaking_at) >= cfg.unduck_idle_s):
                await self._unduck("reply_done")
            elif not self._reply_spoke and (now - self._delivered_at) >= cfg.reply_start_timeout_s:
                await self._unduck("no_reply")
            elif (now - self._delivered_at) >= cfg.unduck_cap_s:
                await self._unduck("cap")

        await self._sync_face()

        if now - self._last_stats_at >= self.config.stats_interval_s:
            self._last_stats_at = now
            self._log_stats()

    async def _sync_face(self) -> None:
        if self._state == "capture":
            want = "listen"
        elif self._state == "confirm":
            want = "think"
        elif self._state == "reply":
            want = "speak" if self._face_speaking else "think"
        else:  # listen
            want = "speak" if self._face_speaking else "idle"

        if want == self._face_sent:
            return
        try:
            await self.send_json({"type": "face", "state": want})
        except Exception as e:  # noqa: BLE001
            logger.debug(f"🎙️ [PuckMic] {self.device} face 送出失敗: {e}")
            return
        self._face_sent = want

    def _log_stats(self) -> None:
        s = self.stats
        logger.info(
            f"🎙️ [PuckMic] {self.device} stats windows={s['windows']} "
            f"duty={s['duty']:.1%} infers={s['infers']} mean_infer_ms={s['mean_infer_ms']:.1f} "
            f"candidates={s['candidates']} confirmed={s['confirmed']} rejected={s['rejected']}")

    # ── PuckVADStream 回呼 ──────────────────────────────────────────────────

    def _vad_on_speech_start(self, pre_roll: bytes) -> None:
        if self._state == "capture" and self._need_new_segment and not self._saw_new_segment:
            self._saw_new_segment = True
            # 指令從這裡才開始：收音上限從現在重算
            self._capture_deadline = self._samples_fed + self.config.capture_max_s * _SAMPLE_RATE
        if self._wake_engine == "swift":
            return
        audio_time = self._samples_fed / _SAMPLE_RATE
        self._wake.on_speech_start(0, audio_time, pre_roll)

    def _vad_on_pcm(self, pcm: bytes) -> None:
        if self._wake_engine == "whisper":
            self._wake.push_pcm(0, pcm)

    def _vad_on_speech_end(self, seg: bytes, reason: str) -> None:
        if self._wake_engine == "whisper":
            self._wake.on_speech_end(0)
        # PTT 按下前就開始的段落、或「嘿馬文」喊完那段，結尾都不是指令的結尾：
        # 要等新段落開始過，才讓 speech_end 收尾（否則等到 _wait_deadline / capture_max_s）
        if self._state == "capture" and (not self._need_new_segment or self._saw_new_segment):
            self._finish_capture(reason)

    # ── WakeStreamDetector 回呼 ─────────────────────────────────────────────

    async def _on_wake(self, user_id: int, first_audio_time: float, text: str) -> None:
        if self._state != "listen":
            return

        self._candidates += 1
        self._require_wake = True
        # 候選文字喚醒詞後面已經有內容＝一口氣講下去，照常這段結尾就收；
        # 只有喚醒詞＝使用者停下來等 ack，要等下一段話（最多 wake_pause_wait_s）
        paused = not strip_through_wake(text)
        logger.info(f"🎙️ [PuckMic] {self.device} 候選喚醒{'（等指令）' if paused else ''}")
        await self._begin_capture(self.config.capture_pre_s, need_new_segment=paused,
                                  wait_s=self.config.wake_pause_wait_s)

    async def force_wake(self, src: str) -> None:
        """puck PTT 強制喚醒：跳過候選喚醒直接收音。

        src="button"（實體鍵）：confirm 不要求喚醒詞。其他（"pause"＝車機暫停鍵，
        來電/切音源也會送）：照常要求 STT 文字含喚醒詞，不是喚醒詞就丟棄。
        """
        if self._state != "listen":
            return
        self._ptt += 1
        self._require_wake = src != "button"
        logger.info(f"🎙️ [PuckMic] {self.device} PTT 強制喚醒（{src}）")
        await self._begin_capture(self.config.ptt_pre_s, need_new_segment=True,
                                  wait_s=self.config.capture_max_s)

    async def _begin_capture(self, pre_s: float, *, need_new_segment: bool, wait_s: float) -> None:
        self._state = "capture"
        self._need_new_segment = need_new_segment
        self._saw_new_segment = False
        self._wait_deadline = self._samples_fed + wait_s * _SAMPLE_RATE

        pre_roll_blocks = math.ceil(pre_s * _SAMPLE_RATE / BLOCK_SAMPLES)
        self._capture_buf = list(self._ring)[-pre_roll_blocks:] if pre_roll_blocks else []
        self._capture_deadline = self._samples_fed + self.config.capture_max_s * _SAMPLE_RATE

        try:
            await self.send_json({"type": "duck"})
            self._ducked = True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"🎙️ [PuckMic] {self.device} duck 送出失敗: {e}")
        await self._sync_face()

    # ── 收音結束 → 確認 ──────────────────────────────────────────────────────

    def _finish_capture(self, reason: str) -> None:
        self._state = "confirm"
        wav_bytes = _build_wav(b"".join(self._capture_buf))
        self._capture_buf = []
        self._confirm_task = self._loop.create_task(self._confirm(wav_bytes))
        self._loop.create_task(self._sync_face())

    async def _confirm(self, wav_bytes: bytes) -> None:
        try:
            text = (await self.confirm_stt(wav_bytes)) or ""
        except Exception as e:  # noqa: BLE001
            logger.warning(f"🎙️ [PuckMic] {self.device} confirm_stt 失敗: {e}")
            text = ""

        has_wake = pinyin_wake_match(text) is not None
        if (self._require_wake and not has_wake) or not text.strip():
            self._rejected += 1
            logger.info(f"🎙️ [PuckMic] {self.device} 否決（STT 無喚醒詞）")
            await self._unduck("rejected")
            return

        self._confirmed += 1
        cmd = strip_through_wake(text)
        final = f"馬文，{cmd}" if cmd else "馬文"
        logger.info(f"🎙️ [PuckMic] {self.device} 指令: {final}")

        self._state = "reply"
        self._delivered_at = self.clock()
        self._reply_spoke = False
        self._last_speaking_at = None
        await self._sync_face()

        try:
            await self.deliver(self.speaker, final)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"🎙️ [PuckMic] {self.device} deliver 失敗: {e}")
            await self._unduck("deliver_error")

    async def _unduck(self, reason: str) -> None:
        if self._ducked:
            try:
                await self.send_json({"type": "unduck"})
            except Exception as e:  # noqa: BLE001
                logger.warning(f"🎙️ [PuckMic] {self.device} unduck 送出失敗: {e}")
            self._ducked = False
        self._state = "listen"
        self._wake.rearm(0)
        logger.info(f"🎙️ [PuckMic] {self.device} unduck（{reason}）")
        await self._sync_face()

    # ── 關閉 ────────────────────────────────────────────────────────────────

    def _dump_write(self, pcm_bytes: bytes) -> None:
        if self._dump is None:
            try:
                os.makedirs(self._dump_dir, exist_ok=True)
                path = os.path.join(self._dump_dir, f"{self.device}_{time.strftime('%Y%m%d_%H%M%S')}.wav")
                self._dump = wave.open(path, "wb")
                self._dump.setnchannels(1)
                self._dump.setsampwidth(2)
                self._dump.setframerate(_SAMPLE_RATE)
                logger.info(f"🎙️ [PuckMic] {self.device} 錄音傾印 → {path}")
            except OSError as e:
                logger.warning(f"🎙️ [PuckMic] {self.device} 開傾印檔失敗，停用: {e}")
                self._dump_dir = ""
                return
        self._dump.writeframes(pcm_bytes)

    async def close(self) -> None:
        if self._dump is not None:
            self._dump.close()
            self._dump = None
        # 只在 STT 確認階段取消；指令已送出（reply）就讓 deliver 跑完，斷線不吃掉使用者的指令
        if (self._state == "confirm" and self._confirm_task is not None
                and not self._confirm_task.done()):
            self._confirm_task.cancel()
            try:
                await self._confirm_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass
        if self._ducked:
            try:
                await self.send_json({"type": "unduck"})
            except Exception:  # noqa: BLE001
                pass
            self._ducked = False
        self._wake.cleanup()
        if hasattr(self._wake, "aclose"):
            await self._wake.aclose()

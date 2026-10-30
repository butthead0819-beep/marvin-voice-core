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
from wake_stream_detector import WakeStreamDetector

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 16000


@dataclass
class PuckMicConfig:
    capture_pre_s: float = 1.5      # 候選喚醒時，往前取這麼多秒原始音訊當指令開頭
    capture_max_s: float = 6.0      # 候選喚醒後最多再收這麼多秒（音樂中 VAD 常不會停）
    speak_hold_s: float = 3.5       # SpeakTracker hold：0.4s ICY + iPhone 播放延遲 ~3s
    reply_start_timeout_s: float = 12.0  # 送出指令後這麼久 Marvin 都沒開口 → unduck
    unduck_idle_s: float = 3.0      # Marvin 開口後，安靜這麼久 → unduck
    unduck_cap_s: float = 30.0      # 送出指令後最長壓低時間
    stats_interval_s: float = 60.0
    ring_s: float = 3.0             # 原始 PCM ring buffer 長度（要 >= capture_pre_s）


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
                 config: Optional[PuckMicConfig] = None):
        self.device = device
        self.speaker = speaker
        self.send_json = send_json
        self.confirm_stt = confirm_stt
        self.deliver = deliver
        self.tts_frames_total = tts_frames_total
        self.clock = clock
        self.config = config or PuckMicConfig()
        self._loop = loop

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

        self._suppressed = False
        self._bad_packets = 0

        self._candidates = 0
        self._confirmed = 0
        self._rejected = 0
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
        mean_infer_ms = (self._infer_elapsed_total / self._infer_count * 1000) if self._infer_count else 0.0
        return {
            "windows": vs["windows"],
            "speech_windows": vs["speech_windows"],
            "duty": duty,
            "infers": self._infer_count,
            "mean_infer_ms": mean_infer_ms,
            "candidates": self._candidates,
            "confirmed": self._confirmed,
            "rejected": self._rejected,
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
            if self._state == "capture":
                self._capture_buf.append(pcm_bytes)

            self._samples_fed += BLOCK_SAMPLES
            self._vad_stream.feed(pcm)

            if self._state == "capture" and self._samples_fed >= self._capture_deadline:
                self._finish_capture("max")

        await self._post_block_checks()

    async def _post_block_checks(self) -> None:
        now = self.clock()
        speaking = self._speak.update(self.tts_frames_total(), now)

        want_suppressed = speaking and self._state == "listen"
        if want_suppressed != self._suppressed:
            self._vad_stream.set_suppressed(want_suppressed)
            self._suppressed = want_suppressed

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

        if now - self._last_stats_at >= self.config.stats_interval_s:
            self._last_stats_at = now
            self._log_stats()

    def _log_stats(self) -> None:
        s = self.stats
        logger.info(
            f"🎙️ [PuckMic] {self.device} stats windows={s['windows']} "
            f"duty={s['duty']:.1%} infers={s['infers']} mean_infer_ms={s['mean_infer_ms']:.1f} "
            f"candidates={s['candidates']} confirmed={s['confirmed']} rejected={s['rejected']}")

    # ── PuckVADStream 回呼 ──────────────────────────────────────────────────

    def _vad_on_speech_start(self, pre_roll: bytes) -> None:
        audio_time = self._samples_fed / _SAMPLE_RATE
        self._wake.on_speech_start(0, audio_time, pre_roll)

    def _vad_on_pcm(self, pcm: bytes) -> None:
        self._wake.push_pcm(0, pcm)

    def _vad_on_speech_end(self, seg: bytes, reason: str) -> None:
        self._wake.on_speech_end(0)
        if self._state == "capture":
            self._finish_capture(reason)

    # ── WakeStreamDetector 回呼 ─────────────────────────────────────────────

    async def _on_wake(self, user_id: int, first_audio_time: float, text: str) -> None:
        if self._state != "listen":
            return

        self._candidates += 1
        self._state = "capture"

        pre_roll_blocks = math.ceil(self.config.capture_pre_s * _SAMPLE_RATE / BLOCK_SAMPLES)
        self._capture_buf = list(self._ring)[-pre_roll_blocks:] if pre_roll_blocks else []
        self._capture_deadline = self._samples_fed + self.config.capture_max_s * _SAMPLE_RATE

        logger.info(f"🎙️ [PuckMic] {self.device} 候選喚醒")

        try:
            await self.send_json({"type": "duck"})
            self._ducked = True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"🎙️ [PuckMic] {self.device} duck 送出失敗: {e}")

    # ── 收音結束 → 確認 ──────────────────────────────────────────────────────

    def _finish_capture(self, reason: str) -> None:
        self._state = "confirm"
        wav_bytes = _build_wav(b"".join(self._capture_buf))
        self._capture_buf = []
        self._confirm_task = self._loop.create_task(self._confirm(wav_bytes))

    async def _confirm(self, wav_bytes: bytes) -> None:
        try:
            text = (await self.confirm_stt(wav_bytes)) or ""
        except Exception as e:  # noqa: BLE001
            logger.warning(f"🎙️ [PuckMic] {self.device} confirm_stt 失敗: {e}")
            text = ""

        if pinyin_wake_match(text) is None:
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

    # ── 關閉 ────────────────────────────────────────────────────────────────

    async def close(self) -> None:
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

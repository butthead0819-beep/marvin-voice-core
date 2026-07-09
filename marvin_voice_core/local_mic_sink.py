"""Local microphone audio source for the Marvin voice pipeline.

Satisfies the AudioSource protocol (protocols.py) — allows the pipeline to
accept audio from the local machine microphone without touching the Discord
RealtimeVADSink path.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Iterable

import numpy as np

from marvin_voice_core.adaptive_noise_floor import AdaptiveNoiseFloor
from marvin_voice_core.audio_utils import calculate_rms

logger = logging.getLogger(__name__)

_LOCAL_USER_ID = "local"

# Must match the >19200-byte gate in marvin_voice_core/sink.py (L176).
_MIN_AUDIO_BYTES = 19200


class LocalMicSink:
    """Minimal local-mic audio source that feeds the existing voice pipeline.

    The *source* parameter accepts an iterable of raw PCM byte chunks (48 kHz
    stereo int16 by default).  In production leave it None; start() opens a
    sounddevice InputStream.  In tests, pass a list of pre-built byte chunks —
    no real hardware required.

    Speech detection uses an adaptive noise floor (AdaptiveNoiseFloor，取樣背景
    RMS、非寫死門檻) plus a *time-based* silence cut：句尾累積靜默達 silence_cut_s
    才切句（預設 1.5s，依 VADGap 實測句內停頓 p95≈1.48s 定，避免自然停頓把整句
    切碎）。時間由 chunk 位元組長度換算（deterministic、不依賴浮動 blocksize）。
    """

    def __init__(
        self,
        on_speech_cut_callback: Callable,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        source: Iterable[bytes] | None = None,
        min_audio_bytes: int = _MIN_AUDIO_BYTES,
        rms_threshold: int = 500,
        silence_cut_s: float = 1.5,
        sample_rate: int = 48000,
        channels: int = 2,
        suppress_wake_callback: Callable[[], bool] | None = None,
        on_speech_start_callback: Callable | None = None,
        user_id: str = _LOCAL_USER_ID,
    ) -> None:
        # user_id：切句 callback 帶的講者鍵。本機模式預設 "local"；Wyoming 橋（衛星麥）
        # 傳自己的（如 "satellite"），讓 speaker_provider 映射到既有身分（記憶延續）。
        self._user_id = user_id
        self.on_speech_cut_callback = on_speech_cut_callback
        self._loop = loop
        self._source = source
        self._min_audio_bytes = min_audio_bytes
        # rms_threshold 現為「靜態最低門檻」＝自適應地板的下限（安靜房間兜底）；
        # 真正驅動偵測的是取樣背景後的 noise_floor+delta（見 AdaptiveNoiseFloor）。
        self._noise_floor = AdaptiveNoiseFloor(static_floor=rms_threshold)
        # 句尾靜默達此秒數才切句（時間基準，非幀數）。依 VADGap 實測 p95≈1.48s。
        self._silence_cut_s = silence_cut_s
        self._sample_rate = sample_rate
        self._channels = channels

        self._speech_buffer: bytearray = bytearray()
        self._is_speaking: bool = False
        self._silence_accum_s: float = 0.0
        self._speech_start_time: float = 0.0
        self._frame_count: int = 0

        # Active-sink interface — lets downstream engine/controller access these
        # attributes directly without AttributeError in local-mode.
        self.meta_analyzer = None
        self.wake_stream = None
        self.user_buffers: dict = {}
        self.user_is_speaking: dict = {}
        self.user_last_spoken_time: dict = {}
        self.user_first_audio_time: dict = {}
        self.user_last_packet_time: dict = {}
        self.user_near_silence_count: dict = {}
        self.user_wake_check_done: dict = {}
        self.user_wake_check_count: dict = {}
        self.user_utt_max_gap: dict = {}
        self.last_audio_packet_time: float = 0.0
        self.suppress_wake_callback: Callable[[], bool] = suppress_wake_callback or (lambda: False)
        # 私語 barge-in：靜默→人聲的起音邊緣同步觸發，讓 device 音訊路徑可打斷。
        self.on_speech_start_callback: Callable = on_speech_start_callback or (lambda _uid: None)

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None:
            return self._loop
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.get_event_loop()

    def _chunk_duration_s(self, chunk: bytes) -> float:
        """由 PCM 位元組長度換算秒（不依賴浮動的 sounddevice blocksize）。
        48k stereo int16：len / (48000 × 2ch × 2bytes) = len / 192000。"""
        return len(chunk) / (self._sample_rate * self._channels * 2)

    def _process_chunk(self, chunk: bytes, timestamp: float) -> None:
        rms = calculate_rms(chunk)
        self._frame_count += 1
        active_threshold = self._noise_floor.update(rms)

        if rms > active_threshold:
            if not self._is_speaking:
                self._is_speaking = True
                self._speech_start_time = timestamp
                self.on_speech_start_callback(self._user_id)
            self._speech_buffer.extend(chunk)
            self._silence_accum_s = 0.0
            # NOTE: 刻意不填 user_last_spoken_time/user_is_speaking——開放麥克風底噪會讓
            # _wait_for_user_silence 永遠判「還在講」而擋住 play_tts。留空 → silence gate 直接放行。
        else:
            if self._is_speaking:
                # 時間基準切句：句尾累積靜默達 silence_cut_s（預設 1.5s）才切。
                # 句內停頓 < 門檻時被下一個人聲 frame 重置，不會把整句切碎。
                self._silence_accum_s += self._chunk_duration_s(chunk)
                if self._silence_accum_s >= self._silence_cut_s:
                    self._cut_segment()

    def _cut_segment(self) -> None:
        audio_data = bytes(self._speech_buffer)
        self._speech_buffer = bytearray()
        self._is_speaking = False
        self._silence_accum_s = 0.0
        speech_start = self._speech_start_time
        self._speech_start_time = 0.0

        if len(audio_data) <= self._min_audio_bytes:
            if self._frame_count % 50 == 0:
                logger.debug("[Core_LocalSink] 片段過短 (%d bytes)，丟棄", len(audio_data))
            return

        # 診斷：切句時的語音時長（只計人聲幀）——判「切太早」是本機 VAD 過早切、
        # 還是 Swift STT 把完整音訊聽殘。短時長=VAD 早切；長時長但文字殘=STT 問題。
        _voiced_s = len(audio_data) / (self._sample_rate * self._channels * 2)
        logger.info("[Core_LocalSink] ✂️ 切句：人聲 %.1fs（靜默門檻 %.1fs）", _voiced_s, self._silence_cut_s)

        self._get_loop().create_task(
            self.on_speech_cut_callback(self._user_id, audio_data, speech_start)
        )

    async def start(self) -> None:
        """Begin audio capture.

        Test mode (source provided): processes each chunk synchronously.
        Production mode (source=None): opens a sounddevice InputStream and
        runs until an internal stop event is set.
        """
        if self._source is not None:
            now = time.time()
            for chunk in self._source:
                self._process_chunk(chunk, now)
            return

        # Production path — lazy import so the test suite never requires sounddevice.
        try:
            import sounddevice as sd  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "[Core_LocalSink] sounddevice 未安裝；"
                "請 pip install sounddevice 或以 source= 注入測試資料。"
            ) from exc

        self._stop_event: asyncio.Event = asyncio.Event()
        loop = self._get_loop()

        def _sd_callback(indata, frames, ctime, status):  # noqa: ANN001
            if status and self._frame_count % 50 == 0:
                logger.warning("[Core_LocalSink] sounddevice status: %s", status)
            stereo = self._mono_to_stereo(bytes(indata))
            loop.call_soon_threadsafe(self._process_chunk, stereo, time.time())

        # channels=1: 通用單聲道擷取（Mac 內建麥克風等不支援 channels=2）
        # callback 內上採樣成 stereo，下游仍收到 48k stereo int16 契約
        stream = sd.InputStream(
            samplerate=self._sample_rate,
            channels=1,
            dtype="int16",
            callback=_sd_callback,
        )
        stream.start()
        logger.info(
            "[Core_LocalSink] 麥克風串流已開啟（%d Hz, mono→stereo upmix）",
            self._sample_rate,
        )
        try:
            await self._stop_event.wait()
        finally:
            stream.stop()
            stream.close()
            logger.info("[Core_LocalSink] 麥克風串流已關閉")

    @staticmethod
    def _mono_to_stereo(frame_bytes: bytes) -> bytes:
        """複製 mono int16 幀成 interleaved stereo（L=R），讓下游仍收到 48k stereo int16。"""
        mono = np.frombuffer(frame_bytes, dtype=np.int16)
        stereo = np.repeat(mono, 2)
        return stereo.tobytes()

    def write(self, user=None, data=None) -> None:  # noqa: ANN001
        """Local-mode no-op: Discord's write path is never called for local mic."""

    def elevate_vad(self, user_id: str = _LOCAL_USER_ID, duration: float = 15.0) -> None:
        """Local-mode no-op: VAD elevation is a Discord-specific mechanism."""

    def _stream_release(self, user_id: str = _LOCAL_USER_ID) -> None:
        """Local-mode no-op: stream-release is a Discord-specific mechanism."""

    def stop(self) -> None:
        """Signal the sounddevice stream to stop (no-op in test/source mode)."""
        if hasattr(self, "_stop_event"):
            self._stop_event.set()

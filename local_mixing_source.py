"""Plan 12 LocalMixingAudioSource — always-on 本地 f32 混音 source。

對 Discord 是一條不中斷的 `AudioSource.read()`：每 20ms 在 discord voice send
thread 上被呼叫，逐幀把 music 層 + TTS 層在 f32 混好（gain / duck / TPDF dither）
再 dither 成 s16。整個語音 session 只有這一條 play()，取代舊的 6 條 vc.play()。

不變量（read() 在 RT voice thread、驅動全部音訊）：
  ⚠ idle 時回 silence frame（3840 bytes 全零），絕不回 None/b""（否則 discord 停播）
  ⚠ read() 內部任何例外 → 回 silence、永不 raise（single point of failure for ALL audio）

並發（OV #2）：producer（event loop）push TTS buffer / set music 走 lock-free——
deque.append/popleft 與單一參考指派在 CPython GIL 下 atomic，read()（consumer）
絕不 acquire 可競用 lock，避免 RT thread 被餓死 glitch music。

DSP 與 offline A/B 共用 audio_mixing module（±2 LSB 已驗證）。
"""
from __future__ import annotations

import collections
import logging

import numpy as np

import audio_mixing as am

logger = logging.getLogger(__name__)

SAMPLE_RATE = 48000
CHANNELS = 2
# discord 一幀 = 20ms：960 sample/ch × 2ch = 1920 interleaved samples
FRAME_SAMPLES = int(SAMPLE_RATE * 0.02) * CHANNELS  # 1920
FRAME_BYTES_S16 = FRAME_SAMPLES * 2                 # 3840
_SAMPLES_PER_SEC = SAMPLE_RATE * CHANNELS

try:
    import discord
    _BASE = discord.AudioSource
except Exception:  # pragma: no cover - discord 一定在，但測試環境保險
    _BASE = object


class LocalMixingAudioSource(_BASE):
    def __init__(
        self,
        *,
        volume: float = 1.0,
        duck_level: float = 0.30,
        duck_step: float = 0.28,
        tts_cap_seconds: float = 30.0,
        seed: int | None = None,
    ):
        self._volume = float(volume)
        self._duck_level = float(duck_level)
        self._duck_step = float(duck_step)
        self._duck_cur = 1.0  # 1.0 = 無 duck
        self._tts_cap_samples = int(tts_cap_seconds * _SAMPLES_PER_SEC)
        self._rng = np.random.default_rng(seed)
        self._silence_bytes = b"\x00" * FRAME_BYTES_S16

        self._music = None                       # 可換 f32le source（atomic ref）
        self._tts_queue: collections.deque = collections.deque()  # 預解碼 f32 buffers
        self._tts_cur: np.ndarray | None = None  # 當前 TTS buffer（consumer-local）
        self._tts_off = 0                         # consumer-local offset

    # ── discord.AudioSource 介面 ──────────────────────────────────────────────

    def is_opus(self) -> bool:
        return False

    def read(self) -> bytes:
        try:
            music_f = self._next_music_frame()
            tts_f = self._next_tts_frame()
            tts_active = tts_f is not None

            # duck ramp：TTS 在 → 往 duck_level 下降；TTS 走 → 回 1.0（逐幀線性、防 click）
            target = self._duck_level if tts_active else 1.0
            if self._duck_cur < target:
                self._duck_cur = min(target, self._duck_cur + self._duck_step)
            elif self._duck_cur > target:
                self._duck_cur = max(target, self._duck_cur - self._duck_step)

            layers = []
            if music_f is not None:
                layers.append(am.apply_gain(music_f, self._volume * self._duck_cur))
            if tts_f is not None:
                layers.append(tts_f)  # TTS gain 1.0
            if not layers:
                return self._silence_bytes

            mixed = am.mix_layers(layers)
            s16 = am.to_s16(am.tpdf_dither(mixed, self._rng))
            return s16.tobytes()
        except Exception:
            logger.exception("[Plan12_Mixer] read() 內部錯誤，回 silence（永不 raise）")
            return self._silence_bytes

    def cleanup(self):  # discord 停播時呼叫；mixer 狀態持久，no-op
        pass

    # ── producer API（event loop thread，lock-free）───────────────────────────

    def set_music_source(self, source) -> None:
        """設音樂層來源（read()→f32le bytes / b"" 表耗盡）。單一參考指派 atomic。"""
        self._music = source

    def clear_music(self) -> None:
        self._music = None

    def set_volume(self, volume: float) -> None:
        """即時音量（下一幀生效，無接縫、無 hotswap）。"""
        self._volume = float(volume)

    def push_tts(self, f32_buffer: np.ndarray) -> bool:
        """把預解碼的 TTS f32 buffer 排進 TTS 層。超過 cap → 拒絕回 False（caller 降級貼文）。"""
        buf = np.asarray(f32_buffer, dtype=np.float32)
        if self._tts_load_samples() + buf.size > self._tts_cap_samples:
            return False
        self._tts_queue.append(buf)
        return True

    # ── 狀態 query（barrier reader 用；T3 讓 cog 兩欄位委派到這）─────────────────

    def is_idle(self) -> bool:
        return self._music is None and self._tts_cur is None and not self._tts_queue

    @property
    def is_playing_audio(self) -> bool:
        return not self.is_idle()

    def tts_load_seconds(self) -> float:
        return self._tts_load_samples() / _SAMPLES_PER_SEC

    @property
    def tts_queue_duration(self) -> float:
        return self.tts_load_seconds()

    # ── 內部（consumer：voice thread）──────────────────────────────────────────

    def _tts_load_samples(self) -> int:
        cur_remain = (self._tts_cur.size - self._tts_off) if self._tts_cur is not None else 0
        return cur_remain + sum(b.size for b in self._tts_queue)

    def _next_music_frame(self) -> np.ndarray | None:
        src = self._music
        if src is None:
            return None
        try:
            buf = src.read()
        except Exception:
            logger.exception("[Plan12_Mixer] music source read 失敗，清空音樂層")
            self._music = None
            return None
        if not buf:  # 耗盡
            self._music = None
            return None
        f = np.frombuffer(buf, dtype=np.float32)
        if f.size < FRAME_SAMPLES:
            f = np.concatenate([f, np.zeros(FRAME_SAMPLES - f.size, dtype=np.float32)])
        elif f.size > FRAME_SAMPLES:
            f = f[:FRAME_SAMPLES]
        return f

    def _next_tts_frame(self) -> np.ndarray | None:
        if self._tts_cur is None:
            if not self._tts_queue:
                return None
            self._tts_cur = self._tts_queue.popleft()
            self._tts_off = 0
        buf = self._tts_cur
        chunk = buf[self._tts_off:self._tts_off + FRAME_SAMPLES]
        self._tts_off += FRAME_SAMPLES
        if self._tts_off >= buf.size:  # 本 buffer 消化完
            self._tts_cur = None
        if chunk.size < FRAME_SAMPLES:  # clip 尾端不足一幀 → 補零（≤20ms 邊界靜音）
            chunk = np.concatenate([chunk, np.zeros(FRAME_SAMPLES - chunk.size, dtype=np.float32)])
        return chunk


class MixerPlaybackAdapter(_BASE):
    """每次 (re)connect 新建的薄 adapter，read()/is_opus() 委派到持久的 mixer。

    OV #4：不重用同一個 AudioSource 物件跨 VoiceClient——discord.py 停播會呼叫
    `source.cleanup()`，跨 client 重用未定義。mixer 狀態（layer/佇列）持久跨 reconnect，
    每次重連交給 vc.play() 一個新 adapter；adapter.cleanup() 不碰 mixer。
    """

    def __init__(self, mixer: LocalMixingAudioSource):
        self._mixer = mixer

    def is_opus(self) -> bool:
        return self._mixer.is_opus()

    def read(self) -> bytes:
        return self._mixer.read()

    def cleanup(self):  # discord 停播時呼叫；不可動持久 mixer 狀態
        pass


class S16ToF32MusicSource:
    """把 s16le AudioSource（discord.FFmpegPCMAudio）即時轉 f32le 給 mixer 音樂層。

    重用既有 ffmpeg（loudnorm / reconnect opts / PositionTracking），不需自建 f32 ffmpeg
    source。轉換在 voice thread 上、純算術。進來的音樂是 full-scale s16（loudnorm 後），
    s16→f32 等同無損；Plan 12 的低音量量化優化發生在 mixer 之後的 f32 gain → 仍成立。
    """

    def __init__(self, s16_source):
        self._src = s16_source

    def read(self) -> bytes:
        buf = self._src.read()
        if not buf:
            return b""
        f = np.frombuffer(buf, dtype=np.int16).astype(np.float32) / np.float32(32768.0)
        return f.tobytes()

    def cleanup(self):
        c = getattr(self._src, "cleanup", None)
        if callable(c):
            c()


def ensure_mixer_playing(voice_client, adapter_factory) -> bool:
    """vc 連線中且未在播 → play 一個新 adapter，回 True；已在播/無 vc → 不動回 False。

    OV #4：用 try/except 兜 discord.py 自身 reconnect 與 watcher 的 AlreadyPlaying race
    （is_playing() 檢查到 play() 之間的 TOCTOU），永不 raise。
    adapter_factory: () -> AudioSource，每次新建不重用。
    """
    if voice_client is None:
        return False
    try:
        if not voice_client.is_connected():
            return False
        if voice_client.is_playing():
            return False
        voice_client.play(adapter_factory())
        return True
    except Exception:
        logger.warning("[Plan12_Mixer] ensure_mixer_playing 略過（vc 狀態競態或未就緒）")
        return False

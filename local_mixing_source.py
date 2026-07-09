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
import threading
import time

import numpy as np

import audio_mixing as am

logger = logging.getLogger(__name__)

SAMPLE_RATE = 48000
CHANNELS = 2
# discord 一幀 = 20ms：960 sample/ch × 2ch = 1920 interleaved samples
FRAME_SAMPLES = int(SAMPLE_RATE * 0.02) * CHANNELS  # 1920
FRAME_BYTES_S16 = FRAME_SAMPLES * 2                 # 3840
FRAME_BYTES_F32 = FRAME_SAMPLES * 4                 # 7680
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
        tts_gain: float = 0.5,
        tts_cap_seconds: float = 30.0,
        seed: int | None = None,
        instrument: bool = False,
        on_demand: bool = False,
        idle_grace_s: float = 1.0,
        clock=None,
    ):
        self._volume = float(volume)
        self._duck_level = float(duck_level)
        self._duck_step = float(duck_step)
        self._tts_gain = float(tts_gain)  # TTS 層增益（音樂常播 ~10%，TTS 滿音量過大 → 預設減半）
        self._duck_cur = 1.0  # 1.0 = 無 duck
        self._wake_duck_until = 0.0  # 🔇 [Wake Duck] 喚醒確認 → 音樂 duck 到此時戳（不等 TTS）
        # 🔇 [TTS 對玩家 duck] Marvin 自己的 TTS（尤其長的：DJ interjection / 歌單理由）播放中
        # 若有玩家還在說話 → TTS 讓路到 10%（同串流音樂）；最後一次說話後 5s 無聲才回 1.0。
        self._clock = clock or time.monotonic
        self._tts_player_duck_level = 0.10
        self._tts_player_duck_hold_s = 5.0
        self._tts_player_duck_step = 0.12   # 逐幀 ramp（~50fps 下 ~0.15s 到位，防 click）
        self._tts_player_duck_cur = 1.0
        self._player_speech_until = 0.0
        self._prev_tts_marvin = False   # 上一幀有無 Marvin TTS（偵測 onset 復原凍結的 duck）
        self._tts_cap_samples = int(tts_cap_seconds * _SAMPLES_PER_SEC)
        self._rng = np.random.default_rng(seed)
        self._silence_bytes = b"\x00" * FRAME_BYTES_S16

        self._paused = False                     # 控制台暫停：read() 回 silence、不前進來源
        self._music = None                       # 可換 f32le source（atomic ref）
        self._tts_queue: collections.deque = collections.deque()  # 預解碼 f32 buffers
        self._tts_cur: np.ndarray | None = None  # 當前 TTS buffer（consumer-local）
        self._tts_off = 0

        # 🎭 [打岔層 layer2] Marmo 在 Marvin 講話尾段「混音疊進來打斷」。獨立佇列，與
        # layer1 並行混音（mix_layers 逐元素相加）、不互相排隊。layer2 活躍時 layer1(Marvin)
        # 壓到 _interject_duck，讓打岔的 Marmo 蓋得過尾巴。
        self._tts2_queue: collections.deque = collections.deque()
        self._tts2_cur: np.ndarray | None = None
        self._tts2_off = 0
        self._interject_duck = 0.6    # layer2 活躍時 layer1 的目標增益（fade 終點）。
        # 0.6 不是 0.45：用戶回饋 0.45 下 Marvin「完全退位」被 Marmo 蓋掉；0.6 讓他還在、
        # 只是被蓋過（漫才被吐槽的感覺），不是消失。
        # layer1 在 layer2 進來時「逐漸 fade out」而非瞬降。逐幀線性 ramp，step 0.008/幀 →
        # 1.0→0.6 約 1.0s 平滑淡出（用戶回饋再慢一點點）。
        self._interject_cur = 1.0
        self._interject_step = 0.008

        # instrumentation（flag-gated；每 5s 印 [Plan12_Stats]，供 live 判 mixer 是否跟得上）
        # on-demand：idle 超過 grace → read() 回 b"" 讓 discord 停送（修 always-on×DAVE）。
        # 內容到達時 caller 重新 vc.play(adapter)，每段播放=獨立 player thread（仿舊路徑 discrete play）。
        self._on_demand = bool(on_demand)
        self._idle_grace_frames = max(1, int(idle_grace_s / 0.02))
        self._idle_count = 0
        self._instrument = bool(instrument)
        self._stat_frames = 0
        self._stat_ms_sum = 0.0
        self._stat_ms_max = 0.0
        self._stat_slow = 0          # read() > 18ms 的幀數（逼近 20ms deadline）
        self._stat_t0 = time.monotonic()

    def note_player_speech(self) -> None:
        """🔇 玩家說話 → 接下來 hold 秒內把 Marvin 正播的長播報 duck 到 player_duck_level。
        speech-detection 路徑每次偵測到玩家說話就呼叫（與 last_player_speech_time 同步）。

        **barge-in 閘**：只在 Marvin 正播 TTS（佇列有料）時才 arm。Marvin 沒在講時玩家說話
        不是打斷——緊接的 ack／回應是全新 onset，該全音量播出；無條件 arm 會把它壓成
        「前小後大」（點歌 ack 前段被壓、5s 窗中途過期才 ramp 回滿）。"""
        if self._tts_cur is None and not self._tts_queue:
            return  # Marvin 沒在講 → 不 arm，避免壓到緊接的回應/ack
        self._player_speech_until = self._clock() + self._tts_player_duck_hold_s

    def duck_for_wake(self, hold_s: float = 5.0) -> None:
        """🔇 [Wake Duck] 喚醒確認 → 立刻把音樂 duck（不等回話 TTS），hold 秒數。

        複用 TTS duck 的 _duck_level 與逐幀 ramp（平滑不 click）；hold 夠長橋接到回話
        TTS 接手（避免 LLM 期間彈回再 duck 的 bounce）。給即時「我聽到你了」回饋。"""
        self._wake_duck_until = self._clock() + hold_s

    def _music_duck_target(self, tts_active: bool) -> float:
        """音樂 duck 目標增益：TTS 播放中 或 喚醒 duck hold 內 → _duck_level，否則 1.0。"""
        wake_duck = self._clock() < self._wake_duck_until
        return self._duck_level if (tts_active or wake_duck) else 1.0

    def _tts_player_duck_step_toward(self, now: float) -> float:
        """逐幀 ramp TTS 對玩家說話的 duck 增益：說話窗內 → 往 10% 降；窗外（5s 無聲）→ 回 1.0。"""
        target = self._tts_player_duck_level if now < self._player_speech_until else 1.0
        cur = self._tts_player_duck_cur
        if cur < target:
            cur = min(target, cur + self._tts_player_duck_step)
        elif cur > target:
            cur = max(target, cur - self._tts_player_duck_step)
        self._tts_player_duck_cur = cur
        return cur

    def set_interject_params(self, *, duck: float | None = None, step: float | None = None) -> None:
        """即時調打岔 duck 終點 / fade 速度（taste-tuning 用，免重啟）。"""
        if duck is not None:
            self._interject_duck = max(0.0, min(1.0, float(duck)))
        if step is not None:
            self._interject_step = max(0.001, min(1.0, float(step)))

    # ── discord.AudioSource 介面 ──────────────────────────────────────────────

    def is_opus(self) -> bool:
        return False

    def read(self) -> bytes:
        if not self._instrument:
            return self._read_impl()
        t0 = time.perf_counter()
        out = self._read_impl()
        self._record_stat((time.perf_counter() - t0) * 1000.0)
        return out

    def _record_stat(self, dt_ms: float) -> None:
        self._stat_frames += 1
        self._stat_ms_sum += dt_ms
        if dt_ms > self._stat_ms_max:
            self._stat_ms_max = dt_ms
        if dt_ms > 18.0:
            self._stat_slow += 1
        now = time.monotonic()
        if now - self._stat_t0 >= 5.0 and self._stat_frames:
            avg = self._stat_ms_sum / self._stat_frames
            mst = self._music.stats() if hasattr(self._music, "stats") else {}
            # 用 print 直寫 stdout（對齊 [TTS_TIMING] 慣例）——local_mixing_source logger
            # 沒被設成 INFO，logger.info 會被吞
            print(
                f"[Plan12_Stats] {now - self._stat_t0:.1f}s f={self._stat_frames} "
                f"read_ms(avg/max)={avg:.2f}/{self._stat_ms_max:.2f} slow>18ms={self._stat_slow} "
                f"music_underrun={mst.get('underruns', '-')} "
                f"buf={mst.get('depth', '-')}/{mst.get('max', '-')} tts_q={len(self._tts_queue)}",
                flush=True,
            )
            self._stat_frames = 0
            self._stat_ms_sum = 0.0
            self._stat_ms_max = 0.0
            self._stat_slow = 0
            self._stat_t0 = now

    def clear_tts(self) -> None:
        """丟棄所有待播/當前 TTS（使用者打斷時用）。否則打斷的 TTS 會殘留在佇列累積亂播。"""
        self._tts_queue.clear()
        self._tts_cur = None
        self._tts_off = 0
        self._tts2_queue.clear()  # 打岔層一起清
        self._tts2_cur = None
        self._tts2_off = 0

    def set_paused(self, paused: bool) -> None:
        """控制台暫停/續播：暫停時 read() 回 silence 但不前進音樂/TTS 來源（保位置、保 adapter 不停）。"""
        self._paused = bool(paused)

    def _read_impl(self) -> bytes:
        try:
            if self._paused:
                return self._silence_bytes  # 持位置、adapter 續活（不進 idle→b"" 邏輯）
            music_f = self._next_music_frame()
            tts_f = self._next_tts_frame()
            tts2_f = self._next_tts2_frame()  # 打岔層（Marmo）
            tts_active = tts_f is not None or tts2_f is not None

            # duck ramp：TTS 在 或 喚醒 duck hold 內 → 往 duck_level 下降；否則回 1.0（逐幀線性、防 click）
            target = self._music_duck_target(tts_active)
            if self._duck_cur < target:
                self._duck_cur = min(target, self._duck_cur + self._duck_step)
            elif self._duck_cur > target:
                self._duck_cur = max(target, self._duck_cur - self._duck_step)

            # 打岔 duck ramp：layer2(Marmo) 在 → layer1(Marvin) 逐幀往 _interject_duck 淡出；
            # layer2 走 → 逐幀回 1.0。線性、防突兀（用戶回饋：瞬降太快，要漸進 fade out）。
            _itarget = self._interject_duck if tts2_f is not None else 1.0
            if self._interject_cur < _itarget:
                self._interject_cur = min(_itarget, self._interject_cur + self._interject_step)
            elif self._interject_cur > _itarget:
                self._interject_cur = max(_itarget, self._interject_cur - self._interject_step)

            layers = []
            if music_f is not None:
                layers.append(am.apply_gain(music_f, self._volume * self._duck_cur))
            # 🔇 TTS 對玩家說話 duck：玩家最近說話 → Marvin TTS 讓路到 10%，逐幀 ramp（防 click）
            # onset 復原：新一段 Marvin TTS 進來、且無人說話（窗已過）→ 把 idle 期間凍結的 duck
            # 復原 1.0（前幀無 TTS＝靜音，直接設不會 click），避免下段 TTS 殘留壓低。
            _tts_now = tts_f is not None
            if _tts_now and not self._prev_tts_marvin and self._clock() >= self._player_speech_until:
                self._tts_player_duck_cur = 1.0
            self._prev_tts_marvin = _tts_now
            _pd = self._tts_player_duck_step_toward(self._clock())
            if tts_f is not None:
                # 套 tts_gain（音樂 ~10% 時 TTS 滿音量過大）；淡出中再乘 interject_cur；玩家說話再乘 _pd。
                _g = self._tts_gain * self._interject_cur if self._interject_cur < 1.0 else self._tts_gain
                _g *= _pd
                layers.append(am.apply_gain(tts_f, _g))  # Marvin
            if tts2_f is not None:
                layers.append(am.apply_gain(tts2_f, self._tts_gain))  # Marmo（同為 TTS）
            if not layers:
                # idle：always-on 回 silence（永不停）；on-demand 超過 grace 回 b""（discord 停送）
                if self._on_demand:
                    self._idle_count += 1
                    if self._idle_count > self._idle_grace_frames:
                        return b""
                return self._silence_bytes
            self._idle_count = 0  # 有內容 → 重設 idle 計數

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
        """設音樂層來源（read()→f32le bytes / b"" 表耗盡）。先 atomic swap 再清舊源。"""
        old = self._music
        self._music = source  # voice thread 立即讀到新源
        if old is not None and old is not source:
            self._cleanup_source(old)

    def clear_music(self) -> None:
        old = self._music
        self._music = None
        if old is not None:
            self._cleanup_source(old)

    @staticmethod
    def _cleanup_source(source) -> None:
        c = getattr(source, "cleanup", None)
        if callable(c):
            try:
                c()
            except Exception:
                pass

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

    def push_tts2(self, f32_buffer: np.ndarray) -> bool:
        """打岔層（layer2，Marmo）：與 layer1 並行混音、不互相排隊。超過 cap → False。"""
        buf = np.asarray(f32_buffer, dtype=np.float32)
        cur2 = (self._tts2_cur.size - self._tts2_off) if self._tts2_cur is not None else 0
        if cur2 + sum(b.size for b in self._tts2_queue) + buf.size > self._tts_cap_samples:
            return False
        self._tts2_queue.append(buf)
        return True

    # ── 狀態 query（barrier reader 用；T3 讓 cog 兩欄位委派到這）─────────────────

    def is_idle(self) -> bool:
        return (self._music is None
                and self._tts_cur is None and not self._tts_queue
                and self._tts2_cur is None and not self._tts2_queue)

    def has_music(self) -> bool:
        """音樂層是否還在播（來源未耗盡）。caller 等歌播完用。"""
        return self._music is not None

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
        cur2 = (self._tts2_cur.size - self._tts2_off) if self._tts2_cur is not None else 0
        return (cur_remain + sum(b.size for b in self._tts_queue)
                + cur2 + sum(b.size for b in self._tts2_queue))

    def _next_tts2_frame(self) -> np.ndarray | None:
        if self._tts2_cur is None:
            if not self._tts2_queue:
                return None
            self._tts2_cur = self._tts2_queue.popleft()
            self._tts2_off = 0
        buf = self._tts2_cur
        chunk = buf[self._tts2_off:self._tts2_off + FRAME_SAMPLES]
        self._tts2_off += FRAME_SAMPLES
        if self._tts2_off >= buf.size:
            self._tts2_cur = None
        if chunk.size < FRAME_SAMPLES:
            chunk = np.concatenate([chunk, np.zeros(FRAME_SAMPLES - chunk.size, dtype=np.float32)])
        return chunk

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


class BufferedF32MusicSource:
    """背景執行緒預讀 f32 音源進有界 deque，把 mixer.read()（voice thread）跟
    ffmpeg pipe latency 解耦——修 T5 串流斷續（同步讀網路 ffmpeg pipe 卡住整個 mix）。

    contract 對齊音樂層：read() → f32le frame bytes / b""（真耗盡）。
    **underrun（buffer 空但未 eof）回 silence frame、不回 b""**，否則 mixer 會誤判耗盡停歌。
    """

    def __init__(self, inner_source, buffer_frames: int = 50):
        self._inner = inner_source
        self._buf: collections.deque = collections.deque()
        self._maxlen = max(2, int(buffer_frames))
        self._eof = False
        self._stop = False
        self._underruns = 0  # read() 因 buffer 空（未 eof）回 silence 的次數
        self._silence = b"\x00" * FRAME_BYTES_F32
        self._thread = threading.Thread(target=self._fill_loop, daemon=True)
        self._thread.start()

    def stats(self) -> dict:
        return {"underruns": self._underruns, "depth": len(self._buf), "max": self._maxlen}

    def _fill_loop(self):
        while not self._stop:
            if len(self._buf) >= self._maxlen:
                time.sleep(0.005)  # buffer 滿 → backpressure（不丟舊幀）
                continue
            try:
                chunk = self._inner.read()
            except Exception:
                logger.exception("[Plan12_Mixer] buffered 音源 inner.read 失敗，視為 eof")
                self._eof = True
                return
            if not chunk:
                self._eof = True
                return
            self._buf.append(chunk)

    def read(self) -> bytes:
        if self._buf:
            return self._buf.popleft()
        if self._eof:
            return b""
        self._underruns += 1
        return self._silence  # underrun：填空保歌不停（不可回 b""）

    def cleanup(self):
        self._stop = True
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)
        c = getattr(self._inner, "cleanup", None)
        if callable(c):
            try:
                c()
            except Exception:
                pass


def ensure_mixer_playing(device, adapter_factory) -> bool:
    """device 連線中且未在播 → arm_mixer 一個新 adapter，回 True；已在播/無 device → 不動回 False。

    OV #4：用 try/except 兜 discord.py 自身 reconnect 與 watcher 的 AlreadyPlaying race
    （is_playing() 檢查到 arm_mixer() 之間的 TOCTOU），永不 raise。
    adapter_factory: () -> AudioSource，每次新建不重用。
    """
    if device is None:
        return False
    try:
        if not device.is_connected():
            return False
        if device.is_playing():
            return False
        device.arm_mixer(adapter_factory())
        return True
    except Exception:
        logger.warning("[Plan12_Mixer] ensure_mixer_playing 略過（vc 狀態競態或未就緒）", exc_info=True)
        return False

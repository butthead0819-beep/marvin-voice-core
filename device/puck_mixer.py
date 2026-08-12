"""
puck_mixer.py — 車puck mk2 BT A2DP 混音執行層（Pi 端，零依賴外的 numpy/pyalsaaudio）。

Mac 端 tail_dj_fire_delay() 決定「何時」crossfade，這裡純執行「怎麼」crossfade：
queue_next() 背景解析+緩衝下一首、crossfade() 對兩軌套線性增益 envelope。
單一 process 內部混音（bluealsa PCM 一次只能一個 client 開，見
project_car_puck_mk2_pi_zero2w_bt_mixer_validated 記憶，不可用多個播放器各自接裝置）。
"""
import subprocess
import threading
import time

import numpy as np

from bpm_estimate import estimate_bpm_from_pcm
from scripts.gen_dj_sfx import gen_scratch_from_pcm

try:
    import alsaaudio
except ImportError:  # 開發機沒有 pyalsaaudio，允許 import 供 crossfade_gains() 單元測試
    alsaaudio = None

RATE = 48000
CHANNELS = 2
CHUNK_FRAMES = 1024
BYTES_PER_CHUNK = CHUNK_FRAMES * CHANNELS * 2
# queue_next() 讀先頭這麼多 chunk（約 2.1s）給刷碟合成當原料，同時存進
# peek_buf 供播放補播——不是額外多讀，只是把「反正要播的音訊」提前讀出來用。
SCRATCH_PEEK_CHUNKS = 100
SCRATCH_GAIN = 0.1  # 比照 Mac 端 play_dj_on_tts_layer(path, peak=0.1)，別搶戲


def crossfade_gains(elapsed: float, duration: float) -> tuple:
    """回傳 (gain_a, gain_b)，elapsed/duration 秒，線性 crossfade。
    elapsed<=0 → (1,0)；elapsed>=duration → (0,1)；duration<=0 → 立即切到 b。"""
    if duration <= 0:
        return (0.0, 1.0)
    frac = max(0.0, min(1.0, elapsed / duration))
    return (1.0 - frac, frac)


def resolve_stream_url(watch_url: str, timeout: float = 30.0) -> str:
    out = subprocess.run(
        ["yt-dlp", "-f", "bestaudio", "-g", watch_url],
        capture_output=True, text=True, timeout=timeout,
    )
    if out.returncode != 0:
        raise RuntimeError(f"yt-dlp 解析失敗 {watch_url}: {out.stderr.strip()[-300:]}")
    return out.stdout.strip().splitlines()[0]


def _make_decoder(stream_url: str):
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-i", stream_url,
        "-ar", str(RATE), "-ac", str(CHANNELS), "-f", "s16le", "-",
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE)


def _read_chunk(proc) -> np.ndarray:
    data = proc.stdout.read(BYTES_PER_CHUNK)
    if not data:
        return np.zeros(CHUNK_FRAMES * CHANNELS, dtype=np.int16)
    arr = np.frombuffer(data, dtype=np.int16)
    if len(arr) < CHUNK_FRAMES * CHANNELS:
        arr = np.pad(arr, (0, CHUNK_FRAMES * CHANNELS - len(arr)))
    return arr


def _read_chunk_deck(deck: dict) -> np.ndarray:
    """讀一個 chunk：優先吃 deck["peek_buf"]（queue_next() 為刷碟合成讀先頭時
    順便存下的樣本），吃完才繼續從 proc.stdout 讀——peek 只是把播放本來就要讀的
    音訊提前讀出來，這裡補播回去，不會漏掉那段。"""
    need = CHUNK_FRAMES * CHANNELS
    buf = deck.get("peek_buf")
    if buf is not None and len(buf) > 0:
        if len(buf) >= need:
            chunk = buf[:need]
            deck["peek_buf"] = buf[need:]
            return chunk
        rest = _read_chunk(deck["proc"])
        chunk = np.concatenate([buf, rest])[:need]
        deck["peek_buf"] = None
        return chunk
    return _read_chunk(deck["proc"])


def _mix_scratch(mixed: np.ndarray, scratch, pos: int, gain: float):
    """把刷碟音效疊進已算好的 A/B chunk。回傳 (疊好的 chunk, 新的 pos)；
    scratch 是 None（沒 armed）時新 pos 也回傳 None。播完了（new_pos >= len(scratch)）
    由呼叫端自己比較長度判斷、清掉 armed 狀態——這裡只管疊音量、不管生命週期。"""
    if scratch is None:
        return mixed, None
    n = len(mixed)
    take = min(n, len(scratch) - pos)
    seg = scratch[pos:pos + take].astype(np.float32)
    if take < n:
        seg = np.pad(seg, (0, n - take))
    out = np.clip(mixed.astype(np.float32) + seg * gain, -32768, 32767).astype(np.int16)
    return out, pos + take


class PuckMixer:
    """車puck BT 混音執行層。public method thread-safe、非阻塞（重活丟背景 thread）。"""

    REF_BUFFER_SECONDS = 2.0

    def __init__(self, bt_mac: str):
        self._device = f"bluealsa:DEV={bt_mac},PROFILE=a2dp"
        self._lock = threading.Lock()
        self._loop_thread = None
        self._stop_flag = threading.Event()
        self._deck_a = None  # {"url":..., "proc":...}
        self._deck_b = None
        self._crossfade_start = None  # time.time()，None=沒在轉場
        self._crossfade_duration = 4.0
        self._current_url = None
        self._next_url = None
        # crossfade() 點火時若 deck_b 已合成好刷碟音效就 armed；None=沒疊
        self._scratch_samples = None
        self._scratch_pos = 0
        # AEC reference ring buffer：存最近 REF_BUFFER_SECONDS 秒送進喇叭前的
        # PCM（48kHz stereo interleaved），給 puck_aec.py 當 bit-exact reference。
        self._ref_lock = threading.Lock()
        self._ref_frames = int(RATE * self.REF_BUFFER_SECONDS)
        self._ref_ring = np.zeros(self._ref_frames * CHANNELS, dtype=np.int16)
        self._ref_write_frame = 0  # 下一筆要寫入的 frame 位置（ring index）
        self._ref_frames_written = 0  # 總共寫了多少 frame（供對齊判斷是否已填滿）

    def _append_reference(self, mixed: np.ndarray):
        """把剛混好、即將送喇叭的 PCM 存進 ring buffer（_loop 每個 chunk 呼叫一次）。"""
        n_frames = len(mixed) // CHANNELS
        with self._ref_lock:
            if n_frames > self._ref_frames:
                mixed = mixed[-(self._ref_frames * CHANNELS) :]
                n_frames = self._ref_frames
            start = self._ref_write_frame
            end = start + n_frames
            if end <= self._ref_frames:
                self._ref_ring[start * CHANNELS:end * CHANNELS] = mixed
            else:
                first_len = self._ref_frames - start
                self._ref_ring[start * CHANNELS:] = mixed[: first_len * CHANNELS]
                self._ref_ring[: (end - self._ref_frames) * CHANNELS] = mixed[first_len * CHANNELS :]
            self._ref_write_frame = end % self._ref_frames
            self._ref_frames_written += n_frames

    def recent_reference(self, seconds: float) -> np.ndarray:
        """回傳最近 `seconds` 秒的 stereo interleaved reference（舊到新排序）。
        不足的部分（剛啟動、buffer 還沒填滿）回傳實際可用的長度，可能較短。"""
        with self._ref_lock:
            want_frames = min(int(RATE * seconds), self._ref_frames, self._ref_frames_written)
            if want_frames <= 0:
                return np.zeros(0, dtype=np.int16)
            end = self._ref_write_frame
            start = (end - want_frames) % self._ref_frames
            if start < end:
                return self._ref_ring[start * CHANNELS : end * CHANNELS].copy()
            return np.concatenate(
                [self._ref_ring[start * CHANNELS :], self._ref_ring[: end * CHANNELS]]
            )

    def status(self) -> dict:
        with self._lock:
            return {
                "playing": self._current_url,
                "next_queued": self._next_url,
                "crossfading": self._crossfade_start is not None,
            }

    def play(self, url: str):
        """硬啟動/硬換：終止舊迴圈，單軌重新開播（開播/skip 用）。"""
        self.stop()
        proc = _make_decoder(resolve_stream_url(url))
        with self._lock:
            self._deck_a = {"url": url, "proc": proc}
            self._deck_b = None
            self._current_url = url
            self._next_url = None
            self._crossfade_start = None
        self._ensure_loop_running()

    def queue_next(self, url: str):
        """背景解析+起 ffmpeg 緩衝下一首，不打斷目前播放。順便讀先頭
        SCRATCH_PEEK_CHUNKS（約 2.1s）存進 peek_buf——這段本來就要播，只是提前
        讀出來估 BPM、合成這首專屬的刷碟音效，_read_chunk_deck() 播放時會先吃
        peek_buf 補回去，不會漏音。合成失敗就沒有 scratch_pcm，crossfade() 時這輪
        單純不疊刷碟聲，不擋轉場。"""
        def _load():
            try:
                stream_url = resolve_stream_url(url)
                proc = _make_decoder(stream_url)
            except Exception:
                return
            deck = {"url": url, "proc": proc, "peek_buf": None, "scratch_pcm": None}
            with self._lock:
                self._deck_b = deck
                self._next_url = url
            try:
                peek_buf = np.concatenate([_read_chunk(proc) for _ in range(SCRATCH_PEEK_CHUNKS)])
            except Exception:
                return
            scratch_pcm = None
            try:
                stereo_f32 = peek_buf.reshape(-1, CHANNELS).astype(np.float32)
                bpm = estimate_bpm_from_pcm(stereo_f32.mean(axis=-1), RATE)
                scratch_mono = np.clip(gen_scratch_from_pcm(stereo_f32, rate=RATE, bpm=bpm), -1.0, 1.0)
                scratch_pcm = np.repeat((scratch_mono * 32767).astype(np.int16), CHANNELS)
            except Exception:
                pass
            with self._lock:
                if self._deck_b is deck:  # 沒被下一輪 queue_next() 取代
                    deck["peek_buf"] = peek_buf
                    deck["scratch_pcm"] = scratch_pcm
        threading.Thread(target=_load, daemon=True).start()

    def crossfade(self, duration_s: float = 4.0):
        with self._lock:
            if self._deck_b is None:
                raise RuntimeError("沒有已 queue_next 的下一首可轉場")
            self._crossfade_duration = duration_s
            self._crossfade_start = time.time()
            scratch_pcm = self._deck_b.get("scratch_pcm")
            self._scratch_samples = scratch_pcm
            self._scratch_pos = 0

    def stop(self):
        self._stop_flag.set()
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=2.0)
        with self._lock:
            for deck in (self._deck_a, self._deck_b):
                if deck is not None:
                    deck["proc"].terminate()
            self._deck_a = None
            self._deck_b = None
            self._current_url = None
            self._next_url = None
            self._crossfade_start = None
            self._scratch_samples = None
            self._scratch_pos = 0
        self._stop_flag = threading.Event()
        self._loop_thread = None

    def _ensure_loop_running(self):
        if self._loop_thread is None or not self._loop_thread.is_alive():
            self._stop_flag.clear()
            self._loop_thread = threading.Thread(target=self._loop, daemon=True)
            self._loop_thread.start()

    def _loop(self):
        pcm = alsaaudio.PCM(
            alsaaudio.PCM_PLAYBACK, alsaaudio.PCM_NORMAL, device=self._device,
            channels=CHANNELS, rate=RATE, format=alsaaudio.PCM_FORMAT_S16_LE,
            periodsize=CHUNK_FRAMES,
        )
        try:
            while not self._stop_flag.is_set():
                with self._lock:
                    deck_a, deck_b = self._deck_a, self._deck_b
                    cf_start, cf_dur = self._crossfade_start, self._crossfade_duration
                if deck_a is None:
                    time.sleep(0.05)
                    continue
                a = _read_chunk_deck(deck_a).astype(np.float32)
                if cf_start is not None and deck_b is not None:
                    gain_a, gain_b = crossfade_gains(time.time() - cf_start, cf_dur)
                    b = _read_chunk_deck(deck_b).astype(np.float32)
                    mixed = np.clip(a * gain_a + b * gain_b, -32768, 32767).astype(np.int16)
                    if gain_b >= 1.0:
                        with self._lock:
                            deck_a["proc"].terminate()
                            self._deck_a = deck_b
                            self._deck_b = None
                            self._current_url = self._next_url
                            self._next_url = None
                            self._crossfade_start = None
                else:
                    mixed = np.clip(a, -32768, 32767).astype(np.int16)
                with self._lock:
                    scratch, scratch_pos = self._scratch_samples, self._scratch_pos
                if scratch is not None:
                    mixed, new_pos = _mix_scratch(mixed, scratch, scratch_pos, SCRATCH_GAIN)
                    with self._lock:
                        if self._scratch_samples is scratch:  # 沒被下一次 crossfade() 換掉
                            if new_pos >= len(scratch):
                                self._scratch_samples = None
                                self._scratch_pos = 0
                            else:
                                self._scratch_pos = new_pos
                self._append_reference(mixed)
                pcm.write(mixed.tobytes())
        finally:
            pcm.close()

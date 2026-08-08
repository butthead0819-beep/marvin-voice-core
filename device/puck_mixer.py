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

try:
    import alsaaudio
except ImportError:  # 開發機沒有 pyalsaaudio，允許 import 供 crossfade_gains() 單元測試
    alsaaudio = None

RATE = 48000
CHANNELS = 2
CHUNK_FRAMES = 1024
BYTES_PER_CHUNK = CHUNK_FRAMES * CHANNELS * 2


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
        """背景解析+起 ffmpeg 緩衝下一首，不打斷目前播放。"""
        def _load():
            try:
                stream_url = resolve_stream_url(url)
                proc = _make_decoder(stream_url)
            except Exception:
                return
            with self._lock:
                self._deck_b = {"url": url, "proc": proc}
                self._next_url = url
        threading.Thread(target=_load, daemon=True).start()

    def crossfade(self, duration_s: float = 4.0):
        with self._lock:
            if self._deck_b is None:
                raise RuntimeError("沒有已 queue_next 的下一首可轉場")
            self._crossfade_duration = duration_s
            self._crossfade_start = time.time()

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
                a = _read_chunk(deck_a["proc"]).astype(np.float32)
                if cf_start is not None and deck_b is not None:
                    gain_a, gain_b = crossfade_gains(time.time() - cf_start, cf_dur)
                    b = _read_chunk(deck_b["proc"]).astype(np.float32)
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
                self._append_reference(mixed)
                pcm.write(mixed.tobytes())
        finally:
            pcm.close()

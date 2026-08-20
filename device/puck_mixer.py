"""
puck_mixer.py — 車puck mk2 BT A2DP 播放執行層（Pi 端）。

2026-08-20 架構性重寫：換歌決策/DJ口白全部搬回 Mac 端那顆 mixer——見
main_satellite.py::setup_satellite 的 TeeSpeakerOutput + /audio_stream 說明，
這條路本來就是 ESP32 car puck 真正在響的音訊路徑（Mac 混好的音訊連續廣播出去，
裝置端純粹「有訊號就播」），pi_bt 現在共用同一套。這支檔案不再認得「歌」的
概念，也不再自己 resolve YouTube/跑 yt-dlp/decode 每一首歌——純粹連續消費
/audio_stream（Mac mixer 輸出，chunked MP3），解碼寫進 BT ALSA，像收音機一樣。

舊版的 deck_a/deck_b/play()/queue_next()/crossfade()/speak()（Pi 自己 edge-tts
合成口白）/本地 FIRE 判斷全部拿掉——那整套「Mac 送指令、Pi 自己 resolve+decode+
crossfade」架構反覆踩到 deck 尾段被腰斬、口白跟換歌時機各自一個時鐘對不上，
詳見 project_car_puck_mk2_pi_zero2w_bt_mixer_validated 記憶。

BT ALSA 輸出/重連（_open_pcm*/_write_with_reconnect/_maybe_switch_bt_target）
維持不變——這段完全跟「上游餵什麼 PCM」無關，喇叭端連線問題（沒配對/斷線/
目標裝置切換）不管播放模式是什麼都一樣要處理。
"""
import json
import os
import queue
import subprocess
import threading
import time
import urllib.request

import numpy as np

try:
    import alsaaudio
except ImportError:  # 開發機沒有 pyalsaaudio，允許 import 供單元測試
    alsaaudio = None

RATE = 48000
CHANNELS = 2
CHUNK_FRAMES = 1024
BYTES_PER_CHUNK = CHUNK_FRAMES * CHANNELS * 2

# 2026-08-20：測試用開關——沒有任何候選 BT 裝置在線時，_open_pcm() 卡在跟
# bluealsa 談 A2DP transport 永遠失敗。開這個旗標時完全跳過 alsaaudio/bluealsa，
# 用一個吃光所有 write() 的假 PCM，讓 _loop() 真的跑起來，可以在家測完整
# Mac↔Pi 網路+解碼路徑。正式播放（有 BT 裝置在線）不要開。
NULL_SINK = os.getenv("MARVIN_PUCK_NULL_SINK", "").strip() == "1"


class _NullPCM:
    """MARVIN_PUCK_NULL_SINK=1 專用假 PCM——吃掉所有 write()，不驗證/不輸出任何
    真實音訊。用 sleep 模擬真實 PCM write() 那段時間，讓消耗速度貼近真正播放
    （CHUNK_FRAMES/RATE 秒一個 chunk）。"""

    def write(self, data):
        time.sleep(len(data) / (RATE * CHANNELS * 2))
        return len(data)

    def close(self):
        pass


# 2026-08-20：8s→20s——Pi Zero 2W 有 512MB RAM，20s buffer 也才 ~3.7MB，完全不
# 吃緊；換來扛更長的網路斷流/BT 重連不用整個重連 /audio_stream。代價是延遲（車上
# 聽到的內容落後 Mac 那頭 mixer 實際輸出 ~20s）——收音機模式不是互動對話，這點
# 延遲感覺不出來，換斷流韌性划算。
PREFETCH_SECONDS = 20.0
PREFETCH_CHUNKS = max(1, int(PREFETCH_SECONDS * RATE / CHUNK_FRAMES))
_QUEUE_GET_TIMEOUT_S = 0.5
# 上游斷線（ffmpeg 讀到真正 EOF/連不上 Mac）後多久重連一次——固定小延遲，避免
# Mac 短暫不可達時忙迴圈狂打 /audio_stream。
_RECONNECT_DELAY_S = 2.0
_TITLE_POLL_INTERVAL_S = 3.0


# 2026-08-19：BT 輸出目標從「固定 MAC」改成「候選清單 + 動態挑選」——PuckMixer
# 自己在每次(重)連線時即時重新挑選，才是真正的動態切換。
def _parse_connected_macs(bluetoothctl_output: str) -> set:
    """解析 `bluetoothctl devices Connected` 的輸出，每行長這樣
    `Device AA:BB:CC:DD:EE:FF BMW 04900`，抓第二個欄位的 MAC（正規化成大寫）。"""
    macs = set()
    for line in bluetoothctl_output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "Device":
            macs.add(parts[1].upper())
    return macs


def _list_connected_bt_macs(timeout: float = 5.0) -> set:
    """跑 `bluetoothctl devices Connected` 查目前有連線的 BT MAC。抓不到（指令不在/
    逾時）就回傳空集合——當成「沒有連線資訊可用」處理，不是硬錯誤。"""
    try:
        out = subprocess.run(
            ["bluetoothctl", "devices", "Connected"],
            capture_output=True, text=True, timeout=timeout,
        ).stdout
    except Exception:
        return set()
    return _parse_connected_macs(out)


def pick_bt_mac(candidates: list, connected: set = None):
    """candidates 依優先權排序（第一個優先權最高，實務上＝BMW車機，使用者拍板
    「BMW跟其他喇叭不會同時連線，真的都連著時BMW優先」）。回傳目前有連線、優先權
    最高的候選；查不到任何連線資訊（bluetoothctl 沒回應/都沒連）就照樣回傳優先權
    最高的那個——交給既有的 `_write_with_reconnect` 重試邏輯把連線談起來，不要
    因為偵測失敗就整條不開機。candidates 為空回傳 None。"""
    if not candidates:
        return None
    if connected is None:
        connected = _list_connected_bt_macs()
    for mac in candidates:
        if mac.upper() in connected:
            return mac
    return candidates[0]


# 2026-08-18：Mac 端 :8790 是 main_satellite.py 的 text HTTP server（/audio_stream、
# /car_now 都掛在這個 app 上，見該檔案 build_text_app()）。
MAC_BASE_URL = os.getenv("MARVIN_MAC_BASE_URL", "http://100.123.68.86:8790")
MAC_TOKEN = os.getenv("MARVIN_MAC_TOKEN", "").strip() or os.getenv("MARVIN_VOL_TOKEN", "").strip() or None


def audio_stream_url() -> str:
    """Mac 端 /audio_stream 的完整網址（含 token）——ffmpeg 直接對這個 URL 讀，
    跟讀一般 HTTP 檔案完全一樣（chunked transfer 對 ffmpeg 是透明的）。"""
    from urllib.parse import urlencode
    params = {"t": MAC_TOKEN} if MAC_TOKEN else {}
    qs = f"?{urlencode(params)}" if params else ""
    return f"{MAC_BASE_URL}/audio_stream{qs}"


def fetch_car_now_title(timeout: float = 3.0) -> "str | None":
    """輪詢 Mac 端 /car_now 拿目前播放曲名，餵給 AVRCP metadata（車機螢幕顯示曲名）。
    跟音訊本身完全脫鉤的旁路——查不到（連線失敗/沒在播）回 None，呼叫端不更新
    現有 title，不是硬錯誤。"""
    from urllib.parse import urlencode
    params = {"t": MAC_TOKEN} if MAC_TOKEN else {}
    qs = f"?{urlencode(params)}" if params else ""
    url = f"{MAC_BASE_URL}/car_now{qs}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return None
    if not data.get("playing"):
        return None
    return (data.get("title") or "").strip() or None


def _process_rss_kb() -> "int | None":
    """讀這個 process 目前實際佔用的實體記憶體（VmRSS，KB）——只有 Linux 有
    /proc/self/status，開發機/macOS 讀不到就回 None（診斷用途，不是核心功能，
    讀不到不該影響任何播放邏輯）。"""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])  # 格式："VmRSS:    12345 kB"
    except Exception:
        pass
    return None


def _make_decoder(stream_url: str):
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-i", stream_url,
        "-ar", str(RATE), "-ac", str(CHANNELS), "-f", "s16le", "-",
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE)


def _read_pcm_chunk_or_none(proc):
    """讀一個 chunk；回 None 代表 stdout 真的讀到空——`proc.stdout` 是
    blocking pipe，只有 ffmpeg 寫完關閉 pipe（連線斷了/串流結束）才會讀到空，
    呼叫端可以放心把 None 當成「這個 decoder 真的結束了，該重連」的明確訊號。"""
    data = proc.stdout.read(BYTES_PER_CHUNK)
    if not data:
        return None
    arr = np.frombuffer(data, dtype=np.int16)
    if len(arr) < CHUNK_FRAMES * CHANNELS:
        arr = np.pad(arr, (0, CHUNK_FRAMES * CHANNELS - len(arr)))
    return arr


def _reader_loop(proc, q: "queue.Queue", stop_event: threading.Event):
    """背景 thread：持續從 ffmpeg stdout 讀 chunk 塞進 buffer queue，直到
    stop_event 被喊停、讀取本身出錯，或讀到真正的 EOF——三種都安靜結束這條
    thread。呼叫端（_loop）靠 `thread.is_alive()` 判斷是否已經斷線該重連，不用
    額外的 eof_event（單一持續串流沒有「這首歌播完、下一首接手」的中間狀態要
    分辨，跟舊版多 deck 架構不同）。"""
    while not stop_event.is_set():
        try:
            chunk = _read_pcm_chunk_or_none(proc)
        except Exception:
            return
        if chunk is None:
            return
        while not stop_event.is_set():
            try:
                q.put(chunk, timeout=0.1)
                break
            except queue.Full:
                continue


class PuckMixer:
    """車puck BT 播放執行層。public method thread-safe、非阻塞（重活丟背景 thread）。"""

    REF_BUFFER_SECONDS = 2.0
    # 運行中重新評估 BT 輸出目標的間隔——太短會讓 bluetoothctl subprocess 呼叫
    # 拖累音訊迴圈（每次都是幾十ms的阻塞），太長切換不夠即時；15s 跟
    # device/car-puck-mk2-btspk-autoconnect.sh 重連迴圈的節奏對齊。
    BT_RECHECK_INTERVAL_S = 15.0

    def __init__(self, bt_mac, on_track_change=None):
        # bt_mac：依優先權排序的 BT MAC 候選清單（見 pick_bt_mac()）；也接受單一
        # 字串（沿用舊參數名 bt_mac 保持呼叫端相容）。
        self._candidates = [bt_mac] if isinstance(bt_mac, str) else list(bt_mac)
        self._current_mac = None  # 最近一次實際連上的 MAC，供 status()/log 用
        self._last_bt_check = 0.0
        self._bt_check_inflight = False
        self._pending_bt_target = None
        # AVRCP metadata 掛勾：title 輪詢到變化時呼叫，None＝不裝。
        self._on_track_change = on_track_change
        self._current_title = None
        self._lock = threading.Lock()
        self._loop_thread = None
        self._stop_flag = threading.Event()
        self._connected = False
        self._title_thread = None
        self._title_stop = threading.Event()
        # 2026-08-20：buffer 水位追蹤——決定 PREFETCH_SECONDS 該加大還是減小的
        # 依據。_min_fill_frac：這次連線以來看過的最低水位（越接近 0 代表越
        # 逼近真的斷播/underrun，該加大）；每次重連（_connect_stream）重置，
        # 舊連線的水位不該污染新連線的判斷。current queue 存 self._queue（弱
        # 引用心態——_loop() 每輪重連都會換掉，status() 讀當下這份就好，不用鎖，
        # 讀到「換手瞬間」的舊值也無妨，只是診斷用途，不影響播放）。
        self._queue = None
        self._min_fill_frac = None
        # AEC reference ring buffer：存最近 REF_BUFFER_SECONDS 秒送進喇叭前的
        # PCM（48kHz stereo interleaved），給 puck_aec.py 當 bit-exact reference。
        self._ref_lock = threading.Lock()
        self._ref_frames = int(RATE * self.REF_BUFFER_SECONDS)
        self._ref_ring = np.zeros(self._ref_frames * CHANNELS, dtype=np.int16)
        self._ref_write_frame = 0  # 下一筆要寫入的 frame 位置（ring index）
        self._ref_frames_written = 0  # 總共寫了多少 frame（供對齊判斷是否已填滿）

    def _append_reference(self, mixed: np.ndarray):
        """把剛送喇叭的 PCM 存進 ring buffer（_loop 每個 chunk 呼叫一次）。"""
        n_frames = len(mixed) // CHANNELS
        with self._ref_lock:
            if n_frames > self._ref_frames:
                mixed = mixed[-(self._ref_frames * CHANNELS):]
                n_frames = self._ref_frames
            start = self._ref_write_frame
            end = start + n_frames
            if end <= self._ref_frames:
                self._ref_ring[start * CHANNELS:end * CHANNELS] = mixed
            else:
                first_len = self._ref_frames - start
                self._ref_ring[start * CHANNELS:] = mixed[: first_len * CHANNELS]
                self._ref_ring[: (end - self._ref_frames) * CHANNELS] = mixed[first_len * CHANNELS:]
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
                return self._ref_ring[start * CHANNELS: end * CHANNELS].copy()
            return np.concatenate(
                [self._ref_ring[start * CHANNELS:], self._ref_ring[: end * CHANNELS]]
            )

    def status(self) -> dict:
        """buffer_fill_pct/buffer_min_fill_pct：這次連線以來 queue 水位的當下值/
        最低點（0~100）——判斷 PREFETCH_SECONDS 該加大還是減小的依據。
        min 常態貼著 100（幾乎沒動用過緩衝）代表網路穩、可以考慮縮小換低延遲；
        min 常態逼近 0（快要見底過）代表緩衝不夠、該加大，不是猜的，是實測。
        rss_kb：process 目前實際佔用記憶體（Linux /proc/self/status 的
        VmRSS）——搭配 buffer_min_fill_pct 一起看，才知道加大 PREFETCH_SECONDS
        的記憶體代價是否真的可忽略（Pi Zero 2W 512MB，理論上完全不缺，這裡量
        真的用了多少，不猜）。讀不到（非 Linux/開發機）回 None。"""
        with self._lock:
            connected, title = self._connected, self._current_title
        q = self._queue
        fill_pct = round(100.0 * q.qsize() / PREFETCH_CHUNKS, 1) if q is not None else None
        min_fill_pct = round(self._min_fill_frac * 100.0, 1) if self._min_fill_frac is not None else None
        return {
            "connected": connected,
            "title": title,
            "buffer_max_seconds": PREFETCH_SECONDS,
            "buffer_fill_pct": fill_pct,
            "buffer_min_fill_pct": min_fill_pct,
            "rss_kb": _process_rss_kb(),
        }

    def start(self):
        """啟動播放迴圈（連 /audio_stream）+ 曲名輪詢迴圈。idempotent，重複呼叫安全。"""
        self._ensure_loop_running()
        self._ensure_title_poll_running()

    def stop(self):
        self._stop_flag.set()
        self._title_stop.set()
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=2.0)
        if self._title_thread is not None:
            self._title_thread.join(timeout=2.0)
        with self._lock:
            self._connected = False
        self._stop_flag = threading.Event()
        self._title_stop = threading.Event()
        self._loop_thread = None
        self._title_thread = None

    def _ensure_loop_running(self):
        if self._loop_thread is None or not self._loop_thread.is_alive():
            self._stop_flag.clear()
            self._loop_thread = threading.Thread(target=self._loop, daemon=True)
            self._loop_thread.start()

    def _ensure_title_poll_running(self):
        if self._title_thread is None or not self._title_thread.is_alive():
            self._title_stop.clear()
            self._title_thread = threading.Thread(target=self._title_poll_loop, daemon=True)
            self._title_thread.start()

    def _title_poll_loop(self):
        while not self._title_stop.is_set():
            title = fetch_car_now_title()
            if title and title != self._current_title:
                with self._lock:
                    self._current_title = title
                if self._on_track_change:
                    self._on_track_change(title)
            self._title_stop.wait(_TITLE_POLL_INTERVAL_S)

    def _open_pcm(self):
        if NULL_SINK:
            # ⚠️ 一定要跟真的 open 一樣設 self._current_mac，不然
            # _maybe_switch_bt_target() 每輪迴圈都會看到 target != None、
            # 誤判成「目標換了」，每個 chunk 都重開一次 PCM，量出來的靜音
            # 全是這個假象造成的，不是真實網路/解碼落差。
            self._current_mac = pick_bt_mac(self._candidates)
            print("🔇 [PuckMixer] MARVIN_PUCK_NULL_SINK=1，跳過 BT PCM，用假輸出量測靜音", flush=True)
            return _NullPCM()
        # 每次(重)連線都重新挑選目標——不是開機時算一次的固定值，家用/車上兩種
        # 候選裝置誰在連線範圍內會隨時間改變。
        target = pick_bt_mac(self._candidates)
        self._current_mac = target
        device = f"bluealsa:DEV={target},PROFILE=a2dp"
        # periods=16 把 HW buffer 拉到 ~340ms，吸收排程抖動（bluealsa 預設 4 periods
        # x 4096 bytes ≈ 85ms 太小，實測 Pi Zero 2W 這種弱 CPU 上偶爾卡過會觸發
        # 「No PCM clients」斷線重連）。
        return alsaaudio.PCM(
            alsaaudio.PCM_PLAYBACK, alsaaudio.PCM_NORMAL, device=device,
            channels=CHANNELS, rate=RATE, format=alsaaudio.PCM_FORMAT_S16_LE,
            periodsize=CHUNK_FRAMES, periods=16,
        )

    def _open_pcm_with_retry(self):
        """開 PCM，失敗（目標 BT 裝置目前沒連線／`No such device`等）就重試到
        成功或被 stop()。stop() 被呼叫、還沒開成功就回傳 None，呼叫端自行判斷
        是否要 return。"""
        attempt = 0
        while not self._stop_flag.is_set():
            try:
                return self._open_pcm()
            except Exception:
                attempt += 1
                time.sleep(min(0.5 * attempt, 3.0))
        return None

    def _write_with_reconnect(self, pcm, data: bytes):
        """送一個 chunk；bluealsa/BT 斷線時 pcm.write() 丟 Broken pipe——改成關掉
        舊 handle、重開一個新的 PCM（bluealsa 會重新跟 BT 端談 A2DP transport），
        重連前這段時間的音訊 chunk 直接丟棄（跟網路抖動丟幀同一種取捨，好過
        整段播放死掉沒人發現）。"""
        try:
            pcm.write(data)
            return pcm
        except Exception:
            pass
        try:
            pcm.close()
        except Exception:
            pass
        new_pcm = self._open_pcm_with_retry()
        if new_pcm is None:  # stop() 被呼叫、放棄重連——_loop() 下一輪會自然收尾
            return pcm
        try:
            new_pcm.write(data)
            print("🔌 [PuckMixer] BT PCM 斷線重連成功", flush=True)
        except Exception:
            pass
        return new_pcm

    def _maybe_switch_bt_target(self, pcm):
        """每 BT_RECHECK_INTERVAL_S 秒重新評估一次候選裝置的連線狀態；如果優先權
        最高的目標已經換了，主動斷開重連到新目標。只有 candidates 有兩個以上才
        值得做這個檢查，單一候選跟改動前行為完全一樣。

        `bluetoothctl` subprocess 查詢放背景 thread 做，不擋 _loop() 播放主迴圈
        （偶爾卡個幾百毫秒到幾秒就會逼近甚至遠超 periods=16 的 ~340ms 緩衝，
        觸發 XRUN）。"""
        if len(self._candidates) < 2:
            return pcm
        now = time.time()
        if now - self._last_bt_check >= self.BT_RECHECK_INTERVAL_S and not self._bt_check_inflight:
            self._last_bt_check = now
            self._bt_check_inflight = True

            def _check():
                try:
                    target = pick_bt_mac(self._candidates)
                finally:
                    self._bt_check_inflight = False
                with self._lock:
                    self._pending_bt_target = target
            threading.Thread(target=_check, daemon=True).start()

        with self._lock:
            target = self._pending_bt_target
        if target is None or target == self._current_mac:
            return pcm
        try:
            pcm.close()
        except Exception:
            pass
        new_pcm = self._open_pcm_with_retry()
        if new_pcm is None:  # stop() 被呼叫——_loop() 下一輪自然收尾
            return pcm
        print(f"🔀 [PuckMixer] BT 目標切換 → {target}", flush=True)
        return new_pcm

    def _connect_stream(self):
        """開一個新的 /audio_stream 連線：起 ffmpeg decoder + 背景 reader thread +
        buffer queue。回傳 (queue, reader_thread, reader_stop)。"""
        q = queue.Queue(maxsize=PREFETCH_CHUNKS)
        reader_stop = threading.Event()
        proc = _make_decoder(audio_stream_url())
        reader_thread = threading.Thread(
            target=_reader_loop, args=(proc, q, reader_stop), daemon=True)
        reader_thread.start()
        self._queue = q
        self._min_fill_frac = None  # 新連線，舊連線的水位紀錄不該延續過來
        return q, reader_thread, reader_stop

    def _loop(self):
        pcm = self._open_pcm_with_retry()
        if pcm is None:
            return
        try:
            while not self._stop_flag.is_set():
                q, reader_thread, reader_stop = self._connect_stream()
                with self._lock:
                    self._connected = True
                while not self._stop_flag.is_set():
                    pcm = self._maybe_switch_bt_target(pcm)
                    try:
                        chunk = q.get(timeout=_QUEUE_GET_TIMEOUT_S)
                    except queue.Empty:
                        self._min_fill_frac = 0.0  # 真的見底了，不用等下面的 qsize() 採樣
                        if not reader_thread.is_alive():
                            break  # 上游斷線（真 EOF/連不上），跳出去重連
                        continue  # 讀者還活著，只是這輪剛好沒新資料，繼續等
                    fill_frac = q.qsize() / PREFETCH_CHUNKS
                    if self._min_fill_frac is None or fill_frac < self._min_fill_frac:
                        self._min_fill_frac = fill_frac
                    self._append_reference(chunk)
                    pcm = self._write_with_reconnect(pcm, chunk.tobytes())
                reader_stop.set()
                with self._lock:
                    self._connected = False
                if self._stop_flag.is_set():
                    break
                print(f"🔇 [PuckMixer] /audio_stream 斷線，{_RECONNECT_DELAY_S:.0f}s 後重連", flush=True)
                time.sleep(_RECONNECT_DELAY_S)
        finally:
            pcm.close()

"""
puck_mixer.py — 車puck mk2 BT A2DP 混音執行層（Pi 端，零依賴外的 numpy/pyalsaaudio）。

Mac 端 tail_dj_fire_delay() 決定「何時」crossfade，這裡純執行「怎麼」crossfade：
queue_next() 背景解析+緩衝下一首、crossfade() 對兩軌套線性增益 envelope。
單一 process 內部混音（bluealsa PCM 一次只能一個 client 開，見
project_car_puck_mk2_pi_zero2w_bt_mixer_validated 記憶，不可用多個播放器各自接裝置）。
"""
import os
import queue
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
# 2026-08-19：queue_next() 曾讀先頭 ~2.1s(SCRATCH_PEEK_CHUNKS=100) PCM 算 BPM+
# 合成刷碟轉場音效，已移除（見 queue_next() docstring：Pi Zero 2W CPU 撐不住，
# 會跟 Deck A 播放搶 CPU 觸發 BT 斷線）。_mix_scratch()/SCRATCH_GAIN 混音路徑
# 保留（scratch_pcm 恆為 None 就自然不觸發，非死碼——之後想在算力夠的硬體
# 上恢復這個效果直接復用）。
SCRATCH_GAIN = 0.1  # 比照 Mac 端 play_dj_on_tts_layer(path, peak=0.1)，別搶戲

# 2026-08-19：診斷用開關——實機還是在 Deck A 尾聲附近偶發斷播，懷疑是尾段
# 附近同時發生的兩件背景工作之一在搶 CPU：DJ 口白 speak()（edge-tts 合成，
# 網路+CPU）或 queue_next() 的 Deck B 預抓（ffmpeg 起手+網路）。BPM/刷碟SFX
# 那段已經砍掉（見 queue_next() docstring），但這兩個還在。輪流關掉其中一個
# 現場測試，縮小範圍——不是永久功能開關，排除完可以拔掉。
DISABLE_SPEAK = os.getenv("MARVIN_PUCK_DISABLE_SPEAK", "").strip() == "1"
DISABLE_PREFETCH = os.getenv("MARVIN_PUCK_DISABLE_PREFETCH", "").strip() == "1"

# 2026-08-17：車puck mk2 BMW 實機踩到「1秒斷續+追趕」——原本 _loop() 直接同步呼叫
# proc.stdout.read()，網路/yt-dlp串流一卡，播放迴圈跟著卡住；訊號恢復時 pipe 裡
# 已經堆了好幾個 chunk，連續讀出來造成「追趕」聽感。修法：背景 reader thread 把
# 解碼出來的 chunk 塞進一個有上限的 queue，_loop() 只跟 queue 打交道，網路抖動被
# 這個緩衝吸收，除非抖動時間超過 buffer 長度才會漏音。
# 2026-08-19：手機熱點下 Pi↔Mac 的 Tailscale 沒 direct connect、relay 走香港
# （`tailscale status` 顯示 relay "hkg"），RTT 150-270ms + jitter ~50ms，1.5s
# 根本扛不住——實機 log 10 分鐘內 81 次「prefetch queue 空了」→ BT PCM 斷線
# 重連（平均每 7.4 秒一次），聽感是斷續變小聲+像換歌。拉大到 5.0s 吸收這種
# relay 等級的抖動；代價是換歌/crossfade 反應會多等幾秒，可接受。
#
# ⚠️ 2026-08-19 第二次拉大：家用 WiFi 下也量到 deck_a 自己的 prefetch queue
# 在 deck_b 剛啟動（queue_next()）那一刻炸出一長串「空了」——時間點精準對上，
# deck_b 的新 reader thread 開始搶 Python GIL（Pi Zero 2W 是 quad-core @1GHz
# 弱 CPU，這台機器今天已經多次證實 CPU 很緊繃：BPM/刷碟SFX、bluetoothctl
# 阻塞都是同一類問題），deck_a 的 reader thread 那幾秒被排擠、跟不上主迴圈
# 消耗的速度。5.0→8.0s 給 deck_a 更多緩衝撐過 deck_b 啟動那幾秒的 GIL 競爭。
PREFETCH_SECONDS = 8.0
PREFETCH_CHUNKS = max(1, int(PREFETCH_SECONDS * RATE / CHUNK_FRAMES))
_QUEUE_GET_TIMEOUT_S = 0.5

# 2026-08-17：DJ 口白傳輸——Mac 只送文字，Pi 自己 Edge TTS 合成+疊播，duck 音樂
# 音量比照 Mac 端 cogs/music_cog.py::MusicCog._DJ_INTERJECTION_VOLUME（=0.30，
# 兩邊保持一致的「口白蓋過音樂」聽感）。TTS_VOICE/RATE/PITCH 沿用 tts_engine.py
# 的 zh-TW-YunJheNeural 預設，讓車上口白跟 Discord/家用聽起來是同一個聲音。
DJ_INTERJECTION_VOLUME = 0.30
TTS_VOICE = "zh-TW-YunJheNeural"
TTS_RATE = "-20%"
TTS_PITCH = "-15Hz"


def crossfade_gains(elapsed: float, duration: float) -> tuple:
    """回傳 (gain_a, gain_b)，elapsed/duration 秒，線性 crossfade。
    elapsed<=0 → (1,0)；elapsed>=duration → (0,1)；duration<=0 → 立即切到 b。"""
    if duration <= 0:
        return (0.0, 1.0)
    frac = max(0.0, min(1.0, elapsed / duration))
    return (1.0 - frac, frac)


# 2026-08-18：pi_bt 改走跟 ESP32 edge端混音（esp32_edge_mix）同一條路——Pi 不再
# 自己碰 YouTube。原本讓 Pi 自己跑 yt-dlp 先後踩兩個坑（同一份
# incident_youtube_403_ip_throttle_2026-08-17 記憶）：①無 cookies 的匿名
# ANDROID_VR client 被來源 IP 節流直接 403；②接上 cookies 修 403 後，deno 解
# JS challenge 在 Pi 這種弱 CPU（quad-core Cortex-A53 @1GHz）上常吃到 ~24s CPU
# time，還跟 Mac 那份 cookies session 共用會被 Google 判定異常整組 rotate 掉，
# 迫使 Pi 只能用專屬靜態 cookies.txt、數週要重新手動匯出一次——維護成本疊在
# 「有時候還是會 403」上面不划算。改成跟 main_satellite.py::handle_puck_deck
# 一樣的路：Mac 用自己已經穩定驗證過的 cookiesfrombrowser session 現場
# resolve+轉碼成 MP3，Pi 純粹當消費端接這條 HTTP 串流——ffmpeg -i 對輸入是
# googlevideo CDN 直連還是 Mac 轉碼串流無感，_make_decoder() 不用改。
# MARVIN_MAC_TOKEN 沿用跟 /wake、/flush 呼叫同一套慣例（見 volume_server.py 的
# MAC_SAY/TOKEN；預設沿用 MARVIN_VOL_TOKEN，跟 systemd unit 裡 MARVIN_TEXT_TOKEN
# 對齊的部署慣例一致）。
#
# ⚠️ 2026-08-18：/puck_deck 的 port 在同一天內改過兩次，這裡把來龍去脈記清楚
# 別再繞回去：
#   v1（誤）：讓 24/7 Discord bot（main_discord.py）自己開一個獨立小 app 服務
#     /puck_deck，port 一度沿用 satellite 文字介面的 8790，撞上另一個常駐進程
#     （browsersatellite）也綁在 8790，讓整支 24/7 bot 在啟動時掛掉。
#   v2（也不對）：port 改獨立的 8792 避開衝突，但仔細想「由 24/7 bot 決策車puck
#     播放」這個方向本身就錯了——car-presence 心跳打的是 satellite 的 :8790（見
#     device/car-puck-mk2-presence-heartbeat.sh），這代表架構設計上車puck的
#     「在場觸發」本來就該由 satellite（com.antigravity.marvin.satellite，
#     main_satellite.py）這個「出門的身體」接手，跟 ESP32(esp32_edge_mix) 同一條
#     路、同一個進程——兩者都該由 satellite 統一 resolve+決策，不是 24/7 Discord
#     bot（那是「聊天陪伴」的身體，跟車puck在場觸發語意上不搭）。
#   v3（現在）：回到 satellite 的 :8790，MARVIN_CAR_HARDWARE 只在 satellite 進程
#     的 env 生效（其餘進程明確 override 成空字串關掉，見 run_bot.py/
#     run_browser_satellite.py 的機器本地 wrapper），確保只有一個進程能決策
#     車puck播放，避免多顆大腦互搶同一台實體裝置（見
#     project_car_puck_mk2_pi_zero2w... 記憶「幽靈換歌」事故）。satellite 沒開
#     （純 discord 模式）車puck就沒人管，需要 `marvin-mode dual` 確保常駐。
MAC_BASE_URL = os.getenv("MARVIN_MAC_BASE_URL", "http://100.123.68.86:8790")
MAC_TOKEN = os.getenv("MARVIN_MAC_TOKEN", "").strip() or os.getenv("MARVIN_VOL_TOKEN", "").strip() or None


def resolve_stream_url(watch_url: str, seek: float = None) -> str:
    """watch_url（youtube 頁面網址）→ Mac /puck_deck 轉發網址（見上方模組註解）。
    這裡不做任何網路呼叫，只是組字串——真正的 yt-dlp resolve+ffmpeg 轉碼在 Mac
    端的 handle_puck_deck 收到 ffmpeg 連進來才觸發，_make_decoder() 開的
    subprocess 直接對這個 URL 讀，跟原本讀 googlevideo 直連網址完全一樣的路徑。
    hw=pi_bt：main_satellite.py::handle_puck_deck 靠這個分辨呼叫端不是
    esp32_edge_mix，跳過幫 ESP32 中央 mixer 配的 volume=0.10 衰減——pi_bt
    這邊已經是直接送去開滿的 bluealsa BT 音量，沒有下游 mixer 再衰減一次。

    seek：2026-08-19 補上，YouTube 熱力圖精華起點（跳過前奏），跟 Mac 端
    handle_puck_deck 既有的 seek 參數共用（原本是給斷線重連用，語意一樣是
    「從第幾秒開始這條串流」）。不帶就是完整原始串流、從第 0 秒播——舊行為
    不變。"""
    from urllib.parse import urlencode
    params = {"url": watch_url, "hw": "pi_bt"}
    if MAC_TOKEN:
        params["t"] = MAC_TOKEN
    if seek:
        params["seek"] = f"{seek:.2f}"
    return f"{MAC_BASE_URL}/puck_deck?{urlencode(params)}"


def _make_decoder(stream_url: str):
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-i", stream_url,
        "-ar", str(RATE), "-ac", str(CHANNELS), "-f", "s16le", "-",
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE)


def _read_pcm_chunk_or_none(proc):
    """讀一個 chunk；回 None 代表 stdout 真的讀到空——`proc.stdout` 是
    blocking pipe，只有 ffmpeg 寫完關閉 pipe（stream 正常播完，不是網路
    暫時沒資料）才會讀到空，呼叫端可以放心把 None 當成「這個 decoder 真的
    結束了」的明確訊號，不是需要補靜音撐過去的暫時抖動。"""
    data = proc.stdout.read(BYTES_PER_CHUNK)
    if not data:
        return None
    arr = np.frombuffer(data, dtype=np.int16)
    if len(arr) < CHUNK_FRAMES * CHANNELS:
        arr = np.pad(arr, (0, CHUNK_FRAMES * CHANNELS - len(arr)))
    return arr


def _read_chunk(proc) -> np.ndarray:
    """相容沒有 prefetch queue 的舊路徑（見 _next_chunk）——那條路徑沒有
    eof_event 可以通知，EOF 只能退回補靜音，沒有更好的選擇。"""
    arr = _read_pcm_chunk_or_none(proc)
    return arr if arr is not None else np.zeros(CHUNK_FRAMES * CHANNELS, dtype=np.int16)


def _reader_loop(proc, q: "queue.Queue", stop_event: threading.Event, eof_event: threading.Event = None,
                  started_at: float = None):
    """背景 thread：持續從 ffmpeg stdout 讀 chunk 塞進 prefetch queue，直到
    stop_event 被喊停、讀取本身出錯（例如 proc 已死、pipe 壞掉），或讀到真正
    的 EOF——三種都安靜結束這條 thread，不吵（比照 _write_with_reconnect 的
    優雅降級哲學）。

    ⚠️ 2026-08-19 實機踩到：改這版之前，EOF 被 _read_chunk() 當成一般的
    「這個 chunk 沒讀到」個案退回補靜音，這裡的迴圈永遠不會停——deck 播完
    之後會無限期灌靜音進 queue，_loop() 完全不知道這首歌已經真的結束了，
    只能乾等 Mac 排定的 crossfade 時間點。YouTube metadata 的 duration 只要
    跟 Mac 端 /puck_deck 實際轉碼出來的串流長度差個幾秒，deck_a 就會在真正
    結尾提早進入「假裝還在播、其實是靜音」的狀態——這就是「尾端提早結束/
    無聲」規律出現在每首歌的真因，跟 DJ 口白/Deck B 預抓都無關（兩者都測試
    排除過了）。現在讀到真正 EOF 會設 eof_event，_loop() 主迴圈可以立刻自己
    判斷要不要扶正 deck_b，不用死等 crossfade 時間點。"""
    _first_chunk = True
    while not stop_event.is_set():
        try:
            chunk = _read_pcm_chunk_or_none(proc)
        except Exception:
            return
        if chunk is None:
            if eof_event is not None:
                eof_event.set()
            return
        if _first_chunk:
            _first_chunk = False
            if started_at is not None:
                print(f"🎧 [PuckMixer] deck 第一個真實 chunk 到手，距 _load() 起算 "
                      f"{time.time() - started_at:.2f}s", flush=True)
        while not stop_event.is_set():
            try:
                q.put(chunk, timeout=0.1)
                break
            except queue.Full:
                continue


def _next_chunk(deck: dict) -> np.ndarray:
    """從 deck 的 prefetch queue 取下一個 chunk；沒有 queue（測試/舊路徑）就退回
    直接同步讀 proc。queue 空了（buffer 也追不上，抖動超過 PREFETCH_SECONDS）就
    補靜音——寧可漏一小段音訊，也不能讓 PCM 寫入迴圈被卡住拖累整條播放時序。

    ⚠️ 2026-08-19：eof_event 已設起來（_reader_loop 讀到真正 EOF、確定不會再
    有新 chunk）時，改用 timeout=0 立刻回靜音，不要再等滿 _QUEUE_GET_TIMEOUT_S
    (=0.5s)——那 0.5 秒是假設「網路/解碼可能還在追」才值得等，deck_a 真正播完
    又沒有 deck_b 可接手時，這個 queue 保證永遠不會再有東西，每輪迴圈還傻等
    0.5 秒等於主播放迴圈每 ~21ms 該寫一次 PCM 的節奏被拖到 500ms 一次，遠超
    periods=16 的緩衝，直接把 BT PCM 拖進斷線重連風暴（實機踩到：「deck_a
    已讀到真正結尾但沒有 deck_b 可接手」那行 log 之後緊接著一長串斷線重連）。"""
    q = deck.get("queue")
    if q is None:
        return _read_chunk(deck["proc"])
    eof_event = deck.get("eof_event")
    timeout = 0 if (eof_event is not None and eof_event.is_set()) else _QUEUE_GET_TIMEOUT_S
    try:
        return q.get(timeout=timeout)
    except queue.Empty:
        print("🔇 [PuckMixer] prefetch queue 空了，補靜音（網路/解碼跟不上）", flush=True)
        return np.zeros(CHUNK_FRAMES * CHANNELS, dtype=np.int16)


def _start_deck_reader(proc, started_at: float = None) -> dict:
    """幫一個剛開好的 decoder proc 起一條 _reader_loop 背景 thread + prefetch
    queue，回傳可以直接 `**` 展開塞進 deck dict 的欄位。eof_event：讀到真正
    EOF（stream 正常播完）時會被設起來，見 _reader_loop docstring。started_at：
    診斷用，見 _reader_loop 的「第一個真實 chunk」log。"""
    q = queue.Queue(maxsize=PREFETCH_CHUNKS)
    reader_stop = threading.Event()
    eof_event = threading.Event()
    reader_thread = threading.Thread(
        target=_reader_loop, args=(proc, q, reader_stop, eof_event, started_at), daemon=True)
    reader_thread.start()
    return {"queue": q, "reader_stop": reader_stop, "reader_thread": reader_thread, "eof_event": eof_event}


def _read_chunk_deck(deck: dict) -> np.ndarray:
    """讀一個 chunk：優先吃 deck["peek_buf"]（queue_next() 為刷碟合成讀先頭時
    順便存下的樣本），吃完才繼續從 prefetch queue 讀——peek 只是把播放本來就要讀的
    音訊提前讀出來，這裡補播回去，不會漏掉那段。"""
    need = CHUNK_FRAMES * CHANNELS
    buf = deck.get("peek_buf")
    if buf is not None and len(buf) > 0:
        if len(buf) >= need:
            chunk = buf[:need]
            deck["peek_buf"] = buf[need:]
            return chunk
        rest = _next_chunk(deck)
        chunk = np.concatenate([buf, rest])[:need]
        deck["peek_buf"] = None
        return chunk
    return _next_chunk(deck)


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


def _mix_tts_ducked(mixed: np.ndarray, tts, pos: int, duck_gain: float):
    """把 DJ 口白疊進已算好的 A/B chunk，同時把音樂 duck 到 duck_gain（模擬 Mac
    端 sidechaincompress 的聽感：口白蓋過音樂，不是疊在滿音量音樂上）。回傳
    (疊好的 chunk, 新的 pos)；tts 是 None（沒 armed）時新 pos 也回傳 None、音樂
    維持原音量不 duck。播完了（new_pos >= len(tts)）由呼叫端自己比較長度判斷、
    清掉 armed 狀態、下一輪音樂音量自動恢復——這裡只管當下這個 chunk。"""
    if tts is None:
        return mixed, None
    n = len(mixed)
    take = min(n, len(tts) - pos)
    seg = tts[pos:pos + take].astype(np.float32)
    if take < n:
        seg = np.pad(seg, (0, n - take))
    ducked_music = mixed.astype(np.float32) * duck_gain
    out = np.clip(ducked_music + seg, -32768, 32767).astype(np.int16)
    return out, pos + take


def _synthesize_tts_pcm(text: str) -> "np.ndarray | None":
    """呼叫 edge-tts CLI 合成一段口白到暫存 mp3，再用既有 ffmpeg decode 路徑
    （_make_decoder/_read_chunk）轉成 RATE/CHANNELS 的 PCM int16 array。逾時/
    edge-tts 未安裝/合成空檔都讓例外往上冒，交給呼叫端（PuckMixer.speak()）
    決定要不要安靜放棄——這支只管「合成」，不管降級策略。"""
    import os as _os
    import tempfile

    fd, tmp_path = tempfile.mkstemp(suffix=".mp3")
    _os.close(fd)
    try:
        subprocess.run(
            # --rate/--pitch 的值以 "-" 開頭（例如 "-20%"），argparse 當成獨立 argv
            # 元素會誤判成一個新選項、報 "expected one argument"——用 --key=value
            # 單一 token 形式才不會被拆開解析（2026-08-17 實機踩到）。
            ["edge-tts", "--voice", TTS_VOICE, f"--rate={TTS_RATE}", f"--pitch={TTS_PITCH}",
             "--text", text, "--write-media", tmp_path],
            capture_output=True, timeout=15, check=True,
        )
        proc = _make_decoder(tmp_path)
        chunks = []
        while True:
            data = proc.stdout.read(BYTES_PER_CHUNK)
            if not data:
                break
            chunks.append(np.frombuffer(data, dtype=np.int16))
        proc.wait(timeout=5)
        if not chunks:
            return None
        return np.concatenate(chunks)
    finally:
        try:
            _os.remove(tmp_path)
        except Exception:
            pass


# 2026-08-19：BT 輸出目標從「固定 MAC」改成「候選清單 + 動態挑選」——原本
# `pick_bt_mac()` 只在 volume_server.py 開機那一刻算一次（見該檔案 PUCK_BT_MAC
# 模組層賦值），mixer 拿到的是寫死的字串，運行中途候選裝置的連線狀態變了
# （例如在家用 Soundcore 開機、開車出門 BMW 進範圍）也不會自動換target，得手動
# 重啟服務。搬到這裡讓 PuckMixer 自己在每次(重)連線時即時重新挑選，才是真正的
# 動態切換。volume_server.py 改成 import 這裡的實作（同一份邏輯只維護一次）。
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
    最高的那個——交給 PuckMixer 既有的 `_write_with_reconnect` 重試邏輯把連線談
    起來，不要因為偵測失敗就整條不開機。candidates 為空回傳 None。"""
    if not candidates:
        return None
    if connected is None:
        connected = _list_connected_bt_macs()
    for mac in candidates:
        if mac.upper() in connected:
            return mac
    return candidates[0]


class PuckMixer:
    """車puck BT 混音執行層。public method thread-safe、非阻塞（重活丟背景 thread）。"""

    REF_BUFFER_SECONDS = 2.0
    # 運行中重新評估 BT 輸出目標的間隔——太短會讓 bluetoothctl subprocess 呼叫
    # 拖累音訊迴圈（每次都是幾十ms的阻塞），太長切換不夠即時；15s 跟
    # device/car-puck-mk2-btspk-autoconnect.sh 重連迴圈的節奏對齊。
    BT_RECHECK_INTERVAL_S = 15.0

    def __init__(self, bt_mac, on_track_change=None):
        # bt_mac：依優先權排序的 BT MAC 候選清單（見 pick_bt_mac()）；也接受單一
        # 字串（沿用舊參數名 bt_mac 保持呼叫端相容，單一字串等同單一候選、行為
        # 跟改動前完全一樣）。
        self._candidates = [bt_mac] if isinstance(bt_mac, str) else list(bt_mac)
        self._current_mac = None  # 最近一次實際連上的 MAC，供 status()/log 用
        self._last_bt_check = 0.0
        self._bt_check_inflight = False
        self._pending_bt_target = None
        # AVRCP metadata 掛勾（見 device/avrcp_media_player.py 開頭說明：BMW
        # 30s 規律斷線疑似跟這台裸串流沒回應曲名查詢有關）。None＝不裝，跟舊行為
        # 完全一致；換歌時（play()/crossfade 換到 deck_b）拿到 title 才會呼叫。
        self._on_track_change = on_track_change
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
        # DJ 口白 TTS deck：speak() 背景合成完成後 armed，None=沒疊（音樂不 duck）
        self._tts_samples = None
        self._tts_pos = 0
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

    def play(self, url: str, title: str = None, seek: float = None):
        """硬啟動/硬換：終止舊迴圈，單軌重新開播（開播/skip 用）。url 是 youtube
        頁面網址，這裡自己 yt-dlp resolve 成直連串流網址（見 resolve_stream_url()
        docstring：曾試過交給 Mac 端 resolve，被 Google IP 檢查擋 403，已 revert）。
        title 給 AVRCP metadata 用（見 __init__ 的 on_track_change）；None 就跳過，
        不清空舊值——比起顯示過期標題，螢幕整個閃空白更糟。seek：YouTube 熱力圖
        精華起點，見 resolve_stream_url() docstring。"""
        self.stop()
        proc = _make_decoder(resolve_stream_url(url, seek=seek))
        with self._lock:
            self._deck_a = {"url": url, "proc": proc, **_start_deck_reader(proc)}
            self._deck_b = None
            self._current_url = url
            self._next_url = None
            self._crossfade_start = None
        self._ensure_loop_running()
        if title and self._on_track_change:
            self._on_track_change(title)

    def queue_next(self, url: str, title: str = None, seek: float = None):
        """背景解析+起 ffmpeg 緩衝下一首，不打斷目前播放。url 跟 play() 一樣是
        youtube 頁面網址，這裡自己 resolve（見 play() docstring）。title 存進
        deck，等 _loop() 真的 crossfade 換手（deck_b 變 deck_a）那刻才觸發
        on_track_change——不在這裡提前報，避免車機螢幕在轉場緩衝期間就搶先
        顯示還沒真的在放的下一首。seek：YouTube 熱力圖精華起點，見
        resolve_stream_url() docstring。

        ⚠️ 2026-08-19：原本這裡會多讀開頭 ~2.1s PCM 算 BPM＋合成這首歌專屬的
        刷碟轉場音效（estimate_bpm_from_pcm/gen_scratch_from_pcm，都是 numpy
        運算）。實機在 PREFETCH 觸發 queue_next() 的當下量到 Deck A 那邊連續
        「prefetch queue 空了」+ BT PCM 斷線重連，時間點精準對上——Pi Zero 2W
        （quad-core A53 @1GHz）這段背景運算跟正在播放 Deck A 的主 mixer 迴圈
        搶 CPU，逼近甚至超過 periods=16 的 ~340ms 緩衝，觸發 XRUN。跟 Mac 端
        排在倒數幾秒觸發 queue_next 完全無關（不管多早/多晚觸發都會撞）。
        pi_bt 直接砍掉這段分析＋刷碟音效，只保留必要的解碼緩衝——peek_buf
        不填時 _read_chunk_deck() 會自動退回直接讀 deck 的 queue，不會漏音。

        MARVIN_PUCK_DISABLE_PREFETCH=1 時整段直接跳過（診斷用，見模組常數
        說明）——deck_b 不會 ready，之後的 crossfade() 會自然丟例外優雅失敗，
        Mac 端已有的回退機制（下一首開頭補硬 play）會接手，不會真的沒聲音，
        只是拿掉 crossfade 平滑轉場的效果，純粹用來排除「是不是這段在搶
        CPU」。"""
        if DISABLE_PREFETCH:
            print("🔇 [PuckMixer] MARVIN_PUCK_DISABLE_PREFETCH=1，跳過本次 queue_next", flush=True)
            return
        def _load():
            # 2026-08-19 診斷用時間戳記——追「deck_b 幾乎每首歌都來不及」，
            # 光看 Mac 端 log 對不準真正卡在 Pi 這邊的哪一段（resolve URL 組字串
            # 是瞬間的，真正的網路/decode 開銷在背景 ffmpeg 行程裡，這裡量的是
            # _load() 本身到起好 reader thread 為止，_reader_loop 另外量第一個
            # 真的讀到 chunk 的時間點，兩段合起來才看得出瓶頸在哪）。
            _t0 = time.time()
            try:
                stream_url = resolve_stream_url(url, seek=seek)
                proc = _make_decoder(stream_url)
            except Exception:
                return
            deck = {
                "url": url, "proc": proc, "peek_buf": None, "scratch_pcm": None, "title": title,
                "_load_started_at": _t0,
                **_start_deck_reader(proc, started_at=_t0),
            }
            with self._lock:
                # ⚠️ 2026-08-19 實機踩到：這裡原本直接覆蓋 self._deck_b，如果上一輪
                # queue_next() 排的 deck_b 從沒被 crossfade()/EOF 自救消費掉（例如
                # 被 skip、或 FIRE 逾時後又排了下一輪），舊 deck 的 ffmpeg proc 跟
                # reader thread 永遠不會停——實機查到車puck背景累積 4 條殭屍
                # ffmpeg，最舊一條掛了快 45 分鐘，持續佔 CPU/網路頻寬，讓後續每次
                # queue_next() 都要跟殭屍搶資源，才是「deck_b 幾乎每首歌都來不及
                # 準備好」的真因（跟 highlight_start_s/CPU 搶佔都無關）。覆蓋前先
                # 清掉舊的。
                stale = self._deck_b
                self._deck_b = deck
                self._next_url = url
            if stale is not None:
                reader_stop = stale.get("reader_stop")
                if reader_stop is not None:
                    reader_stop.set()
                try:
                    stale["proc"].terminate()
                except Exception:
                    pass
        threading.Thread(target=_load, daemon=True).start()

    def crossfade(self, duration_s: float = 4.0):
        """⚠️ 2026-08-19 實機踩到：這裡原本只檢查 deck_b 有沒有 ready，沒檢查
        deck_a——Pi 剛重啟/重連時 deck_a 是 None，這時呼叫端（Mac）先
        queue_next() 成功、再 crossfade() 也成功（deck_b 有），但 _loop()
        `if deck_a is None: continue` 直接跳過所有混音，deck_b 永遠不會被
        扶正，狀態卡在 crossfading=True/playing=None 永久沒聲音。Mac 端的
        「crossfade 失敗就補硬 play」保險機制因此也失效，因為這裡回的是
        成功，不是失敗。deck_a 是 None 時直接把 deck_b 扶正成 deck_a（沒有
        東西可以「淡出」，animate 沒意義），不留在半吊子狀態。

        ⚠️ 同一次實機還發現兩個連帶問題：
        ①這裡忘了更新 self._current_url——status() 的 "playing" 欄位讀的是
        這個，不是 _deck_a，扶正 deck_b 後 "playing" 卻還是舊值/None，容易
        誤判成沒真的扶正。
        ②_loop() 播放主迴圈只有 play() 會啟動（_ensure_loop_running()），
        queue_next()/crossfade() 都不會——Pi 重啟後若 Mac 只送了 queue_next()
        （以為之前已經有歌在播、不需要 play()），_loop() 從沒真正跑起來，
        不管呼叫幾次 crossfade() 都沒有人在處理，永遠卡住。crossfade() 也該
        確保迴圈活著（_ensure_loop_running() 本身 idempotent，重複呼叫安全）。"""
        promoted_title = None
        with self._lock:
            if self._deck_b is None:
                raise RuntimeError("沒有已 queue_next 的下一首可轉場")
            if self._deck_a is None:
                self._deck_a = self._deck_b
                self._deck_b = None
                self._current_url = self._next_url
                self._next_url = None
                self._crossfade_start = None
                promoted_title = self._deck_a.get("title")
            else:
                self._crossfade_duration = duration_s
                self._crossfade_start = time.time()
                scratch_pcm = self._deck_b.get("scratch_pcm")
                self._scratch_samples = scratch_pcm
                self._scratch_pos = 0
        self._ensure_loop_running()
        if promoted_title and self._on_track_change:
            self._on_track_change(promoted_title)

    def speak(self, text: str):
        """DJ 口白：Mac 只送文字過來，這裡背景呼叫 edge-tts 合成+疊進 TTS deck
        （見 project_car_puck_mk2_pi_zero2w_bt_mixer_validated 記憶「DJ口白傳輸
        路線定案」）。非阻塞——合成要打網路，不能卡住呼叫端的 HTTP handler。
        合成失敗（edge-tts 未安裝/網路問題/逾時）就安靜放棄，不擋音樂繼續播，
        跟其他降級路徑（_write_with_reconnect 等）同一套哲學。

        MARVIN_PUCK_DISABLE_SPEAK=1 時整段直接跳過（診斷用，見模組常數
        說明）——純粹排除「是不是 edge-tts 合成在搶 CPU/網路」，這輪就是沒有
        DJ 口白，不影響音樂播放。"""
        if DISABLE_SPEAK:
            print(f"🔇 [PuckMixer] MARVIN_PUCK_DISABLE_SPEAK=1，跳過口白：{text[:20]}", flush=True)
            return
        def _synthesize():
            try:
                pcm = _synthesize_tts_pcm(text)
            except Exception:
                print(f"🔇 [PuckMixer] DJ 口白合成失敗，這輪不放：{text[:20]}", flush=True)
                return
            if pcm is None or not pcm.size:
                return
            with self._lock:
                self._tts_samples = pcm
                self._tts_pos = 0
        threading.Thread(target=_synthesize, daemon=True).start()

    def stop(self):
        self._stop_flag.set()
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=2.0)
        with self._lock:
            for deck in (self._deck_a, self._deck_b):
                if deck is not None:
                    reader_stop = deck.get("reader_stop")
                    if reader_stop is not None:
                        reader_stop.set()
                    deck["proc"].terminate()
            self._deck_a = None
            self._deck_b = None
            self._current_url = None
            self._next_url = None
            self._crossfade_start = None
            self._scratch_samples = None
            self._scratch_pos = 0
            self._tts_samples = None
            self._tts_pos = 0
        self._stop_flag = threading.Event()
        self._loop_thread = None

    def _ensure_loop_running(self):
        if self._loop_thread is None or not self._loop_thread.is_alive():
            self._stop_flag.clear()
            self._loop_thread = threading.Thread(target=self._loop, daemon=True)
            self._loop_thread.start()

    def _open_pcm(self):
        # 每次(重)連線都重新挑選目標——不是開機時算一次的固定值，家用/車上兩種
        # 候選裝置誰在連線範圍內會隨時間改變（見上方模組註解）。
        target = pick_bt_mac(self._candidates)
        self._current_mac = target
        device = f"bluealsa:DEV={target},PROFILE=a2dp"
        # 2026-08-19：沒指定 periods 時 bluealsa 預設給 4 periods x 4096 bytes
        # ≈ 85ms 的 HW buffer（實機 log 量測到的值），車puck mk2 BMW 實測每
        # ~30s 準時被 bluealsa 判定「No PCM clients」斷開重連——懷疑 Pi Zero
        # 2W 這種弱 CPU 上 numpy 混音+GIL 偶爾卡過 85ms 就 XRUN，觸發斷線。
        # periods=16 把 HW buffer 拉到 ~340ms，吸收更大的排程抖動。
        return alsaaudio.PCM(
            alsaaudio.PCM_PLAYBACK, alsaaudio.PCM_NORMAL, device=device,
            channels=CHANNELS, rate=RATE, format=alsaaudio.PCM_FORMAT_S16_LE,
            periodsize=CHUNK_FRAMES, periods=16,
        )

    def _open_pcm_with_retry(self):
        """開 PCM，失敗（目標 BT 裝置目前沒連線／`No such device`等）就重試到
        成功或被 stop()。2026-08-17 car puck mk2 實機踩到：MARVIN_PUCK_BT_MAC
        指到的裝置當下沒連線時 `_open_pcm()` 直接丟例外，若沒人接住會讓整條
        _loop thread 死掉，`status()` 卻繼續謊報在播放——跟 BT 斷線中途重連
        是同一種故障，理應用同一套重試邏輯處理，不分「開場」跟「途中斷線」。
        stop() 被呼叫、還沒開成功就回傳 None，呼叫端自行判斷是否要 return。"""
        attempt = 0
        while not self._stop_flag.is_set():
            try:
                return self._open_pcm()
            except Exception:
                attempt += 1
                time.sleep(min(0.5 * attempt, 3.0))
        return None

    def _write_with_reconnect(self, pcm, data: bytes):
        """送一個 chunk；bluealsa/BT 斷線時 pcm.write() 丟 Broken pipe，2026-08-17
        BMW 04900 實機踩到——原本沒接住，整個 _loop thread 直接死掉，之後
        status() 還一直回報「正在播放」但其實已經沒聲音，靜默失敗。改成關掉
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
        最高的目標已經換了（例如從家用 Soundcore 開機、開車出門 BMW 進了範圍），
        主動斷開重連到新目標——不用等 write() 失敗才被動換（那只在舊目標先斷線
        時才會觸發，兩個候選裝置一度同時在線的情況需要這裡主動偵測）。只有
        candidates 有兩個以上才值得做這個檢查，單一候選跟改動前行為完全一樣。

        ⚠️ 2026-08-19 實機踩到：`pick_bt_mac()` 內部呼叫 `bluetoothctl` subprocess
        （見 _list_connected_bt_macs()，timeout=5.0s），這裡原本直接同步呼叫、
        擋在 _loop() 播放主迴圈裡——多個獨立 A/B 測試（關 DJ 口白、關 Deck B
        預抓）都排除不了的規律性斷播/BT 重連，時間間隔對上這裡的 15s 週期。
        `bluetoothctl` 偶爾卡個幾百毫秒到幾秒（bluetoothd 忙/D-Bus 慢）就會逼近
        甚至遠超 periods=16 的 ~340ms 緩衝，觸發 XRUN。改成背景 thread 做偵測，
        _loop() 只讀最近一次算好的結果，永遠不會被這個 subprocess 卡住；真的
        要切換 BT 目標時才會呼叫 `_open_pcm_with_retry()`（本來就會短暫卡頓，
        切換本身就是有感的事件，可接受）。"""
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

    def _loop(self):
        pcm = self._open_pcm_with_retry()
        if pcm is None:
            return
        try:
            while not self._stop_flag.is_set():
                pcm = self._maybe_switch_bt_target(pcm)
                with self._lock:
                    deck_a, deck_b = self._deck_a, self._deck_b
                    cf_start, cf_dur = self._crossfade_start, self._crossfade_duration
                if deck_a is None:
                    time.sleep(0.05)
                    continue
                eof_event = deck_a.get("eof_event")
                if eof_event is not None and eof_event.is_set():
                    # 2026-08-19：deck_a 真的播完了（見 _reader_loop 註解）。
                    # deck_b 已經 ready 就直接扶正，不用死等 Mac 排定的
                    # crossfade 時間點——duration 估計只要跟實際串流長度差
                    # 幾秒，繼續照舊流程會先讀到一堆補靜音，這裡自己先接手。
                    if deck_b is not None:
                        with self._lock:
                            try:
                                deck_a["proc"].terminate()
                            except Exception:
                                pass
                            self._deck_a = deck_b
                            self._deck_b = None
                            self._current_url = self._next_url
                            self._next_url = None
                            self._crossfade_start = None
                        new_title = deck_b.get("title")
                        if new_title and self._on_track_change:
                            self._on_track_change(new_title)
                        continue
                    elif not deck_a.get("_eof_logged"):
                        deck_a["_eof_logged"] = True
                        print("🔇 [PuckMixer] deck_a 已讀到真正結尾但沒有 deck_b 可接手，補靜音等待", flush=True)
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
                        new_title = deck_b.get("title")
                        if new_title and self._on_track_change:
                            self._on_track_change(new_title)
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
                with self._lock:
                    tts, tts_pos = self._tts_samples, self._tts_pos
                if tts is not None:
                    mixed, new_tts_pos = _mix_tts_ducked(mixed, tts, tts_pos, DJ_INTERJECTION_VOLUME)
                    with self._lock:
                        if self._tts_samples is tts:  # 沒被下一次 speak() 換掉
                            if new_tts_pos >= len(tts):
                                self._tts_samples = None
                                self._tts_pos = 0
                            else:
                                self._tts_pos = new_tts_pos
                self._append_reference(mixed)
                pcm = self._write_with_reconnect(pcm, mixed.tobytes())
        finally:
            pcm.close()

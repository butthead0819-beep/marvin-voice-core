import discord
from discord.ext import commands
import asyncio
import concurrent.futures
import threading
import time
import os
import wave
import numpy as np
import discord.opus
try:
    import davey
except ImportError:
    davey = None
from discord.ext import voice_recv
import logging
from collections import deque
from utils import pre_filter_speech, is_whisper_hallucination
from voice_meta_analyzer import VoiceMetaAnalyzer

logger = logging.getLogger("MarvinBot.Engine")


def patch_voice_recv_key_sync(voice_client) -> None:
    """Discord voice session 換 key 時，voice_recv reader 的 decryptor 不會自動更新，
    導致持續 CryptoError 使 STT 完全失效。
    此函數在 voice_client.listen(sink) 之後立即呼叫，將 decryptor.decrypt_rtp / decrypt_rtcp
    替換為自動同步版本：CryptoError 發生時從 voice_client.secret_key 讀取最新 key 並重試。"""
    try:
        from nacl.exceptions import CryptoError as _CryptoError
    except ImportError:
        logger.warning("[KeySync] nacl 未安裝，無法掛載 CryptoError 自動同步補丁")
        return

    reader = getattr(voice_client, '_reader', None)
    if reader is None:
        return
    decryptor = getattr(reader, 'decryptor', None)
    if decryptor is None or getattr(decryptor, '_key_sync_patched', False):
        return

    orig_rtp = decryptor.decrypt_rtp
    orig_rtcp = decryptor.decrypt_rtcp

    def _synced_decrypt_rtp(packet):
        try:
            return orig_rtp(packet)
        except _CryptoError:
            try:
                new_key = bytes(voice_client.secret_key)
                decryptor.update_secret_key(new_key)
                logger.info("[KeySync] RTP CryptoError → reader secret_key 已同步")
                return orig_rtp(packet)
            except Exception as _e:
                logger.warning(f"[KeySync] RTP key 同步失敗: {_e}")
                raise

    def _synced_decrypt_rtcp(packet_data):
        try:
            return orig_rtcp(packet_data)
        except _CryptoError:
            try:
                new_key = bytes(voice_client.secret_key)
                decryptor.update_secret_key(new_key)
                logger.info("[KeySync] RTCP CryptoError → reader secret_key 已同步")
                return orig_rtcp(packet_data)
            except Exception as _e:
                logger.warning(f"[KeySync] RTCP key 同步失敗: {_e}")
                raise

    decryptor.decrypt_rtp = _synced_decrypt_rtp
    decryptor.decrypt_rtcp = _synced_decrypt_rtcp
    decryptor._key_sync_patched = True
    logger.info("[KeySync] voice_recv decryptor auto key-sync 補丁已掛載")


# 每次說話最多做 3 次 Wake Check，分別在開口後 0.6 / 1.2 / 1.8 秒觸發。
# 0.6s 足以捕捉句首 2-3 音節喚醒詞（原本 1.8s 太慢）。
_WAKE_CHECK_TIMES: tuple[float, ...] = (0.6, 1.2, 1.8)

class ConversationBuffer:
    """
    Marvin 滾動緩衝區 (Operation Social Lubricant)
    存儲最近 4 分鐘的對話，並具備自我摘要功能。
    """
    def __init__(self, max_minutes=6):
        self.max_seconds = max_minutes * 60
        self.history = [] # list of dicts: {"timestamp": float, "speaker": str, "text": str}
        self.summaries = [] # list of strings (Marvin-flavored internal monologues)
        self.has_new_messages = False # 🧬 [APM Economy] 空轉跳過標記
        self.last_consumed_timestamp = 0.0 # 🧬 [Incremental Fix] 追蹤上次成功讀取的時間點
        self.game_mode_cap: float | None = None  # when set, caps VAD silence threshold

    def add_entry(self, speaker, text, timestamp=None):
        if timestamp is None:
            timestamp = time.time()
        
        # 🛡️ [Monotonicity Fix] 確保時間戳絕對遞增，防止並發寫入時因精度問題導致 pop 遺漏
        if self.history and timestamp <= self.history[-1]["timestamp"]:
            timestamp = self.history[-1]["timestamp"] + 0.001
            
        self.history.append({"timestamp": timestamp, "speaker": speaker, "text": text})
        self.has_new_messages = True # 🧬 [APM Economy] 偵測到新對話
        self._prune()

    def _prune(self):
        now = time.time()
        # 移除超過 6 分鐘的舊資料
        self.history = [e for e in self.history if now - e["timestamp"] <= self.max_seconds]

    def get_history(self):
        """[Operation Resilience] 被動觸發剪枝並回傳最新對話流"""
        self._prune()
        return self.history

    def pop_new_entries(self) -> list:
        """
        [5分鐘日誌專用] 取出自上次呼叫後的新增對話，並重置 has_new_messages 旗標。
        回傳的是「增量」，不再是整個 6 分鐘歷史。
        """
        self._prune()
        
        # 🧬 [Incremental] 僅過濾出比上次讀取指標還要新的條目
        new_items = [e for e in self.history if e["timestamp"] > self.last_consumed_timestamp]
        
        if new_items:
            # 更新指標為本次讀取的最後一條時間戳
            self.last_consumed_timestamp = max(e.get("timestamp", 0) for e in new_items)
            
        self.has_new_messages = False
        return new_items


    def get_active_speakers(self, last_seconds=240):
        now = time.time()
        speakers = set(e["speaker"] for e in self.history if now - e["timestamp"] <= last_seconds)
        return list(speakers)

    def get_harvest(self, wake_time: float, before: float, after: float,
                    speaker: str | None = None) -> str:
        """
        [Fast System Harvester] 擷取並拼接指定時間窗口內的文字。
        speaker 指定時只取該說話者的片段，避免 harvest 抓到人與人之間的對話。
        """
        start_time = wake_time - before
        end_time = wake_time + after

        segments = [
            item["text"] for item in self.history
            if start_time <= item["timestamp"] <= end_time
            and (speaker is None or item.get("speaker") == speaker)
        ]
        return " ".join(segments)

    def get_last_n_utterances(self, n: int = 5) -> list:
        """
        [Fast System History] 獲取最近 N 句對話。
        """
        self._prune()
        return self.history[-n:] if self.history else []

    # 🚀 [T-04 Fix] get_context_for_analysis() 已移除。
    # 此方法是廢棄的 buffer_summarizer_loop 的配套遺產，已無任何呼叫點。

    def get_conversation_temperature(self, window_seconds=60) -> float:
        """
        [Operation Dynamic Pulse] 擷取交談溫度，動態決定 VAD 截斷閾值
        High (>8): 2.0s | Normal (3~8): 1.2s | Low (<3): 0.6s
        """
        now = time.time()
        recent_utterances = len([e for e in self.history if now - e["timestamp"] <= window_seconds])
        
        if recent_utterances > 8:
            result = 3.0 # 🛠️ [Golden Ear] 高溫期上調至 3.0s，防止請求風暴
        elif 3 <= recent_utterances <= 8:
            result = 1.5 # 🛠️ [Standard] 中度交談調降至 1.5s，兼顧穩定
        else:
            result = 0.8 # 🛠️ [Quiet] 最低閾值下調至 0.8s，實現閃電回應
        if self.game_mode_cap is not None:
            result = min(result, self.game_mode_cap)
        return result

    # 🚀 [T-04 Fix] rotate_and_summarize() 已移除。
    # buffer_summarizer_loop 已被注釋停用，此函式是其唯一呼叫方，已成孤島。


class RealtimeVADSink(voice_recv.AudioSink):
    """
    基於 voice_recv 的純淨 PCM 切片器 (手動 DAVE 解密版)
    """
    def __init__(self, on_speech_cut_callback, on_speech_start_callback=None, temperature_callback=None, sink_error_callback=None, user_vad_callback=None, suppress_wake_callback=None):
        super().__init__()
        self.on_speech_cut_callback = on_speech_cut_callback
        self.on_speech_start_callback = on_speech_start_callback
        self.temperature_callback = temperature_callback
        self.sink_error_callback = sink_error_callback
        self.user_vad_callback = user_vad_callback  # (user_id: int) -> float，per-user 靜音閾值
        # 串流/電台播放中由外部注入，回傳 True 時抑制喚醒偵測，避免擴音回聲誤觸發
        self.suppress_wake_callback = suppress_wake_callback
        self.meta_analyzer = None    # 由 Engine 注入
        self.wake_stream = None      # P3 WakeStreamDetector，由 Engine 注入
        self.user_buffers = {}
        self.user_last_spoken_time = {} # 🎤 [VAD] 真實發聲時間 (RMS > THRESHOLD)
        self.user_last_packet_time = {} # 📦 [VAD] 封包抵達時間 (兜底用)
        self.RMS_THRESHOLD = 150        # 🛠️ [Wake-Word Reliability] 下調閾值以捕捉語音開頭
        self.RMS_THRESHOLD_STREAM = 450 # 串流播放時提高門檻，過濾擴音回聲
        self.user_near_silence_count = {}
        self.user_first_audio_time = {}
        self.user_wake_check_count = {}  # user_id -> int，本次說話已發出的 wake check 次數
        self.decoders = {}
        self.pre_roll_history = {}      # 📦 [Pre-roll] user_id -> deque of last N packets
        self.PRE_ROLL_MAXLEN = 20       # 400ms (20 * 20ms)
        
        # 🚀 [Adaptive Noise Floor]
        self.user_noise_stats = {}      # user_id -> {'sum_x': 0.0, 'sum_x2': 0.0, 'history': deque(maxlen=75)}
        self.user_noise_floor = {}      # user_id -> current noise floor baseline
        self.user_is_speaking = {}      # user_id -> bool
        self.user_speech_confirm_frames = {}  # user_id -> consecutive frames above speech threshold
        self.SPEECH_START_CONFIRM_FRAMES = max(1, int(os.getenv("TTS_INTERRUPT_CONFIRM_FRAMES", "3")))
        self._user_elevated_vad: dict[int, float] = {}  # user_id -> expiry timestamp

        self.loop = asyncio.get_event_loop()
        # self.harvester_task = self.loop.create_task(self._harvester_loop()) # 🚀 [Watchdog] 準備搬遷至 Engine
        self.packet_count = 0
        self.last_audio_packet_time = time.time() # 🛡️ [Heartbeat]
        self.last_decrypted_audio_time = time.time() # 🛡️ [Operation Sentinel] 僅紀錄解密成功的時間點
        self.debug_audio_packets = os.getenv("DEBUG_AUDIO_PACKETS", "false").lower() == "true"

    def elevate_vad(self, user_id: int, duration: float = 15.0) -> None:
        """短暫拉高指定用戶的 VAD 閾值，用於幻覺偵測後壓制風聲/環境音再次觸發。"""
        self._user_elevated_vad[user_id] = time.time() + duration
        logger.info(f"⬆️ [VAD Elevation] User_{user_id} 閾值上調 {duration:.0f}s（幻覺後環境音抑制）")

    def wants_opus(self) -> bool:
        # 為了手動處理 DAVE 加密，我們要求 voice_recv 給我們解密後的原始 Opus 封包
        return True

    def write(self, user: discord.User | discord.Member | None, data: voice_recv.VoiceData):
        if not user:
            return

        self.packet_count += 1
        self.last_audio_packet_time = time.time() # 🛡️ [Heartbeat]

        if self.debug_audio_packets and (self.packet_count <= 5 or self.packet_count % 100 == 0):
            dave_session = getattr(self.voice_client._connection, 'dave_session', None)
            ready = getattr(dave_session, 'ready', False) if dave_session else "N/A"
            print(f"DEBUG: [Sink.write] Pkt: {self.packet_count}, Session: {dave_session}, Ready: {ready}", flush=True)

        user_id = user.id
        
        try:
            # 1. 取得 DAVE Session (由 discord.py 管理)
            dave_session = getattr(self.voice_client._connection, 'dave_session', None)
            
            # 2. 獲取原始 Opus 數據 (由 voice_recv 從 RTP 網路層解出)
            # data.opus 是由 libnacl/chacha20 解密後的 RTP Payload
            # 在 DAVE 環境下，這仍然是 DAVE-encrypted 的
            opus_data = data.opus
            if not opus_data:
                return

            # 3. 手動 DAVE 解密 (如果有啟動的話)
            final_opus = opus_data
            if dave_session and dave_session.ready:
                try:
                    # 🚀 [Diagnostic] 紀錄加密嘗試
                    final_opus = dave_session.decrypt(user_id, davey.MediaType.audio, opus_data)
                except Exception as e:
                    error_msg = str(e)
                    if "UnencryptedWhenPassthroughDisabled" in error_msg:
                        # 💡 [DAVE Fallback] 某些封包在 DAVE 完全同步前可能是明文，直接透傳
                        final_opus = opus_data
                    else:
                        # 💡 [Operation Sentinel] 真正攔截 DAVE 解密失敗
                        if self.packet_count % 50 == 0:
                            print(f"❌ [DAVE Debug] UserID: {user_id}, Error: {e}", flush=True)
                        
                        low_error = error_msg.lower()
                        if "decrypt" in low_error or "crypto" in low_error:
                            # 🚀 [Sentinel O(1) Optimization] 節流：每 5 秒最多回報一次 DAVE 錯誤
                            # 防止 DAVE 同步初期的爆發性錯誤直接刷爆 Sentinel 計數器
                            now = time.time()
                            if self.sink_error_callback and (now - getattr(self, 'last_dave_error_time', 0) > 5):
                                self.last_dave_error_time = now
                                self.sink_error_callback("decryption_failed")
                        return
            elif self.debug_audio_packets and self.packet_count % 100 == 0:
                print(f"📡 [Sink] DAVE Session 尚未就緒 (Session: {dave_session}, Ready: {getattr(dave_session, 'ready', 'N/A')})，嘗試透傳解碼 (Packet: {self.packet_count})", flush=True)

            self.last_decrypted_audio_time = time.time() # 🛡️ [Heartbeat] 到達此處代表解密成功或無需解密

            # 4. 手動 Opus 解碼為 PCM
            if user_id not in self.decoders:
                print(f"🎹 [Sink] 為使用者 {user.name} 建立新的 Opus Decoder (48k Stereo)", flush=True)
                self.decoders[user_id] = discord.opus.Decoder()
            
            # Decoder.decode 會回報 4k Stereo 16-bit PCM (每 20ms 一幀)
            pcm_bytes = self.decoders[user_id].decode(final_opus)

            if self.packet_count == 1:
                print(f"🚀 [Sink] 捕捉第一筆有效語音（DAVE 手動解密成功）！來源: {user.name}", flush=True)

            if user_id not in self.user_buffers:
                self.user_buffers[user_id] = bytearray()
                
            if user_id not in self.pre_roll_history:
                self.pre_roll_history[user_id] = deque(maxlen=self.PRE_ROLL_MAXLEN)

            self.user_buffers[user_id].extend(pcm_bytes)
            
            # 🚀 [True RMS VAD] 計算此封包的真實音量
            now = time.time()
            try:
                # pcm_bytes 是 48k Stereo 16-bit
                pcm_array = np.frombuffer(pcm_bytes, dtype=np.int16)
                rms = int(np.sqrt(np.mean(pcm_array.astype(np.float32)**2)))
                # 🚀 [Diagnostic] 定期印出音量數值
                if self.debug_audio_packets and self.packet_count % 100 == 0:
                    print(f"📊 [VAD Pulse] User_{user_id} | RMS: {rms} / {self.RMS_THRESHOLD}", flush=True)
            except Exception:
                rms = 0
            
            # 只有音量大於閾值，才更新「最後說話時間」
            suppressing = self.suppress_wake_callback() if self.suppress_wake_callback else False
            
            # --- [Adaptive Noise Floor Implementation] ---
            if user_id not in self.user_noise_stats:
                self.user_noise_stats[user_id] = {'sum_x': 0.0, 'sum_x2': 0.0, 'history': deque(maxlen=75)}
                self.user_noise_floor[user_id] = 50.0
                self.user_is_speaking[user_id] = False
                self.user_speech_confirm_frames[user_id] = 0

            stats = self.user_noise_stats[user_id]
            
            # Deadlock Recovery: 如果 RMS 突然掉到極低（例如救護車噪音消失），強制下調基線
            if rms < self.user_noise_floor[user_id] * 0.4:
                stats['history'].clear()
                stats['sum_x'] = 0.0
                stats['sum_x2'] = 0.0
                self.user_noise_floor[user_id] = float(max(10, rms))
            
            if len(stats['history']) == 75:
                old_rms = stats['history'].popleft()
                stats['sum_x'] -= old_rms
                stats['sum_x2'] -= old_rms ** 2
            
            stats['history'].append(rms)
            stats['sum_x'] += rms
            stats['sum_x2'] += rms ** 2
            
            count = len(stats['history'])
            if count == 75:
                mean_rms = stats['sum_x'] / count
                # 防止浮點數誤差導致 variance 變負數
                variance = max(0.0, (stats['sum_x2'] - (stats['sum_x'] ** 2) / count) / count)
                
                # 平穩度檢查 (std dev < 40，即 variance < 1600)，且不是在講話時才更新背景基線
                # 這樣可以防止一直大叫把背景噪音墊高 (人類叫聲的變異數極高，不會觸發此條件)
                if variance < 1600.0:
                    self.user_noise_floor[user_id] = float(mean_rms)
            
            noise_floor = self.user_noise_floor[user_id]
            
            # 動態閾值 (Dynamic Threshold)
            delta_threshold = 100
            dynamic_threshold = max(self.RMS_THRESHOLD, noise_floor + delta_threshold)
            
            if suppressing:
                dynamic_threshold = max(self.RMS_THRESHOLD_STREAM, dynamic_threshold)
            
            # SNR 守衛: RMS 至少要是底噪的 1.5 倍
            snr_threshold = noise_floor * 1.5
            active_threshold = max(dynamic_threshold, snr_threshold)
            # 冷啟動時 noise floor 還沒學穩，普通風扇/鍵盤/開麥底噪可能先被誤判。
            # 只在初始底噪區間要求更大的能量躍升；真正開口仍會明顯高過此門檻。
            if not self.user_is_speaking[user_id] and count < 75 and noise_floor >= 40:
                active_threshold = max(active_threshold, noise_floor + 300, noise_floor * 3.0)
            # 幻覺偵測後短暫拉高閾值，壓制風聲/環境音再次觸發 STT
            _elev_until = self._user_elevated_vad.get(user_id, 0)
            if _elev_until > now:
                active_threshold = max(active_threshold, self.RMS_THRESHOLD_STREAM + 200)
            elif _elev_until:
                self._user_elevated_vad.pop(user_id, None)
            # --- End Adaptive Noise Floor ---

            if rms > active_threshold:
                self.user_speech_confirm_frames[user_id] = self.user_speech_confirm_frames.get(user_id, 0) + 1
                confirmed_speech = self.user_speech_confirm_frames[user_id] >= self.SPEECH_START_CONFIRM_FRAMES

                if not self.user_is_speaking[user_id]:
                    # 短促尖峰先進緩衝，但不立刻打斷 TTS；連續數幀才視為真正開口。
                    if not confirmed_speech:
                        # 暫不更新 last_spoken，避免單次尖峰後被靜音偵測送去 STT。
                        if self.user_first_audio_time.get(user_id, 0) == 0:
                            self.user_first_audio_time[user_id] = now
                        self.user_near_silence_count[user_id] = 0
                    else:
                        # 🚀 [Speech Start Signal] 通知控制器：發言開始，應中斷當前回應
                        self.user_is_speaking[user_id] = True
                        if self.on_speech_start_callback:
                            self.on_speech_start_callback(user_id)
                            
                        print(f"🎬 [VAD] 偵測到有效人聲 (User_{user_id}, RMS: {rms}, Floor: {noise_floor:.1f}{'【串流模式】' if suppressing else ''})", flush=True)
                        # 🚀 [Pre-roll] 將前導緩衝注入正式緩衝區
                        if user_id in self.pre_roll_history and len(self.pre_roll_history[user_id]) > 0:
                            history_bytes = bytearray()
                            for p in list(self.pre_roll_history[user_id]):
                                history_bytes.extend(p)
                            # 注入到 user_buffers 的最前面 (除了剛剛 extend 進去的當前 pcm_bytes)
                            # 目前 user_buffers 尾端是當前封包，我們要把 history 插入到當前封包之前
                            current_packet_len = len(pcm_bytes)
                            content = self.user_buffers[user_id][:-current_packet_len]
                            current = self.user_buffers[user_id][-current_packet_len:]

                            new_buffer = history_bytes + content + current
                            self.user_buffers[user_id] = new_buffer
                            print(f"📦 [Pre-roll] 已注入 {len(history_bytes)} bytes 前導音訊 (User_{user_id})", flush=True)
                            self.pre_roll_history[user_id].clear() # 注入後清理，避免重複注入

                        # 🚀 [Bug Fix] 紀錄第一次說話時間
                        self.user_first_audio_time[user_id] = now

                        # P3: 啟動串流偵測，帶入 pre-roll 讓首次推理盡早觸發
                        if self.wake_stream and not suppressing:
                            self.wake_stream.on_speech_start(
                                user_id, now, bytes(self.user_buffers[user_id])
                            )
                if confirmed_speech or self.user_is_speaking[user_id]:
                    self.user_last_spoken_time[user_id] = now
                    self.user_near_silence_count[user_id] = 0 # 重置微弱能量

                # 🚀 [Wake Check] 週期性喚醒詞快速通道
                # 在 0.6 / 1.2 / 1.8 秒分三次快照，讓句首喚醒詞最快 ~600ms 就被抓到。
                # 串流播放中停用，避免擴音回聲誤觸發。
                _first_audio = self.user_first_audio_time.get(user_id, 0)
                if not suppressing and _first_audio > 0:
                    _elapsed = now - _first_audio
                    _check_count = self.user_wake_check_count.get(user_id, 0)
                    if _check_count < len(_WAKE_CHECK_TIMES) and _elapsed >= _WAKE_CHECK_TIMES[_check_count]:
                        self.user_wake_check_count[user_id] = _check_count + 1
                        audio_snapshot = bytes(self.user_buffers[user_id])
                        self.loop.create_task(
                            self.on_speech_cut_callback(user_id, audio_snapshot, _first_audio, is_wake_check=True)
                        )
            else:
                self.user_speech_confirm_frames[user_id] = 0
                if rms > 50:
                    # 🛡️ [Prosody] 微弱能量補償：即便低於 VAD，若有基礎起伏也視為「正在思考/吸氣」
                    self.user_near_silence_count[user_id] = self.user_near_silence_count.get(user_id, 0) + 1
                
                # 🚀 [新增] 事件驅動靜音偵測 (改善 Watchdog 0.5s 延遲)
                last_spoken = self.user_last_spoken_time.get(user_id, 0)
                if last_spoken > 0:
                    # 取得當前動態閾值
                    if self.user_vad_callback:
                        stt_vad_threshold = self.user_vad_callback(user_id)
                    elif self.temperature_callback:
                        stt_vad_threshold = self.temperature_callback()
                    else:
                        stt_vad_threshold = 0.8
                    if now - last_spoken > stt_vad_threshold:
                        buffer_bytes = len(self.user_buffers[user_id])
                        if buffer_bytes > 19200:
                            print(f"✂️ [Event-Driven VAD] 偵測到 {stt_vad_threshold}s 靜音 (User_{user_id})，聚合 {buffer_bytes} bytes 並送往 STT。", flush=True)
                            audio_data = bytes(self.user_buffers[user_id])
                            self.user_buffers[user_id] = bytearray()
                            self.user_last_spoken_time[user_id] = 0 # 重置，等待下一段語音
                            self.user_wake_check_count.pop(user_id, None)
                            self.user_is_speaking[user_id] = False
                            if self.wake_stream:
                                self.wake_stream.on_speech_end(user_id)

                            # 異步送往 STT
                            self.loop.create_task(
                                self.on_speech_cut_callback(user_id, audio_data, last_spoken)
                            )
                        else:
                            # 緩衝區太短，視為雜訊
                            # if buffer_bytes > 0:
                            #     print(f"🗑️ [Event-Driven VAD] 緩衝區過短 ({buffer_bytes} bytes)，判定為雜訊。", flush=True)
                            self.user_buffers[user_id] = bytearray()
                            self.user_last_spoken_time[user_id] = 0
                            self.user_first_audio_time[user_id] = 0
                            self.user_is_speaking[user_id] = False
                            if user_id in self.pre_roll_history:
                                self.pre_roll_history[user_id].clear()
                            self.user_wake_check_count.pop(user_id, None)
                            if self.wake_stream:
                                self.wake_stream.on_speech_end(user_id)

            # 🚀 [Prosody] 將數據餵入分析器
            if self.meta_analyzer:
                self.meta_analyzer.add_rms(user_id, rms)
            
            # P3: 確認說話後，每幀推送至串流偵測器
            if self.wake_stream and self.user_is_speaking.get(user_id) and not suppressing:
                self.wake_stream.push_pcm(user_id, pcm_bytes)

            # 📦 [Pre-roll] 無論是否說話，都持續維護滑動窗口
            self.pre_roll_history[user_id].append(pcm_bytes)

            # 168: 更新「最後封包時間」
            self.user_last_packet_time[user_id] = now

        except Exception as e:
            # 💡 [Operation Sentinel] 攔截其他底層編碼或封包異常
            error_msg = str(e).lower()
            if self.packet_count % 50 == 0:
                print(f"⚠️ [Sink.write Warning] {e}", flush=True)
            if ("invalid" in error_msg or "lost" in error_msg) and self.sink_error_callback:
                 # 部分損壞封包暫不計入重啟權重，僅保留日誌
                 pass

    def cleanup(self):
        """🚀 [Lifecycle] 清理 Sink 資源"""
        self.user_buffers.clear()
        self.user_last_spoken_time.clear()
        self.user_last_packet_time.clear()
        self.user_first_audio_time.clear()
        self.pre_roll_history.clear()
        self.decoders.clear()
        self.user_noise_stats.clear()
        self.user_noise_floor.clear()
        self.user_is_speaking.clear()
        self.user_speech_confirm_frames.clear()
        self.user_wake_check_count.clear()
        if self.wake_stream:
            self.wake_stream.cleanup()
        print("🧹 [Sink] 已清理所有使用者緩衝區與解碼器。", flush=True)

    async def _flush_user(self, user_id, timestamp):
        """[Operation Traceability] 將緩衝區送往 Engine 的 STT 處理鏈"""
        if user_id in self.user_buffers and len(self.user_buffers[user_id]) > 0:
            audio_data = bytes(self.user_buffers[user_id])
            self.user_buffers[user_id] = bytearray()
            # 💡 [RMS Guard] 清除時間紀錄，準備下一輪
            self.user_last_spoken_time[user_id] = 0
            self.user_first_audio_time[user_id] = 0
            self.user_is_speaking[user_id] = False
            self.user_speech_confirm_frames[user_id] = 0
            if user_id in self.pre_roll_history:
                self.pre_roll_history[user_id].clear()
            self.user_wake_check_count.pop(user_id, None)
            if self.wake_stream:
                self.wake_stream.on_speech_end(user_id)

            # 回呼 Engine.process_audio_slice
            self.loop.create_task(self.on_speech_cut_callback(user_id, audio_data, timestamp))



class DiscordVoiceEngine:
    """
    Discord 多軌感知層引擎 (discord.py 現代架構)
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.stt_callback = None
        self.speech_start_callback = None
        self.post_summon_callback = None
        self.text_channel_callback = None
        self.game_change_callback = None
        self.dismiss_callback = None
        self.bias_update_callback = None
        self.sink_error_callback = None # 💡 [Sentinel] 新增錯誤回報回呼
        self.stt_lock = asyncio.Semaphore(1)       # full-utterance STT（準確度優先）
        self.wake_stt_lock = asyncio.Semaphore(1)  # wake check 專用（與 full STT 互不阻塞）
        self._full_stt_inflight = 0      # concurrent full-utterance STT count
        self._MAX_FULL_STT_INFLIGHT = 3  # drop full-STT beyond this cap
        self._wake_inflight = 0          # concurrent wake-check STT count
        self._MAX_WAKE_INFLIGHT = 2      # drop wake_check beyond this cap (one per user)
        self.game_dict_string = "" # 🚀 [Operation Jargon Override]
        self.is_listening = True
        self.sink = None # 👁️ [VAD Guard] 持有當前 Sink 參照
        
        # 啟動看門狗 (已移至 start() 方法以避免 AttributeError)
        # self.bot.loop.create_task(self._vad_watchdog())
        
        # 🚀 [Chief Architect Patch] Audio Debounce 緩衝系統
        self.audio_buffers = {} # user_id -> {pcm: bytearray, first_start: float}
        self.audio_timers = {}  # user_id -> Task
        self.MAX_AUDIO_CHUNK_DURATION = 12.0 # 聚合上限：縮減為 12 秒，優化辨識負載
        
        # 🧠 [Operation Social Lubricant] 滾動對話緩衝區
        self.conv_buffer = ConversationBuffer(max_minutes=6)
        self._watchdog_task = None  # 🚀 [T-06 Fix] VAD 看門狗任務追蹤，支援優雅退出
        
        # 🚀 [Operation Prosody Perception] 韻律分析器
        self.meta_analyzer = VoiceMetaAnalyzer()

        # 🧠 [STT Evolution] 根據環境變數選擇 STT 引擎 (Operation Traceability)
        self.stt_engine = os.getenv("STT_ENGINE", "macos").lower() # 🚀 [Default Swap] 預設改為 macos 優先模式
        self.whisper_model = None
        self.debug_vad_heartbeat = os.getenv("DEBUG_VAD_HEARTBEAT", "false").lower() == "true"

        # Zombie-thread guard: semaphore released by the thread itself (not asyncio timeout).
        # New calls check acquire(blocking=False) — if taken, the previous thread is still
        # running and the call is dropped. Max 1 Whisper thread alive at any time.
        self._whisper_thread_sem = threading.Semaphore(1)
        self._whisper_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="whisper-stt"
        )

        # Apple platform (macos/mlx) uses Swift as the only STT engine.
        # Whisper is not loaded: no startup cost, no zombie threads possible.
        _is_apple = self.stt_engine in ("macos", "mlx")
        if _is_apple:
            print("🧠 [Engine] Apple platform 偵測，跳過 Faster-Whisper 載入（Swift-only 模式）。", flush=True)
        else:
            print(f"🧠 [Engine] 正在載入備援 Faster-Whisper (Model: tiny, Device: cpu, Quant: int8)...", flush=True)
            try:
                from faster_whisper import WhisperModel
                self.whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")
                print("✅ [Engine] 備援 Faster-Whisper 已就緒。", flush=True)
            except Exception as e:
                print(f"⚠️ [Engine Warning] Faster-Whisper 載入失敗: {e}", flush=True)

        # P3: WakeStreamDetector — 聲學層實時喚醒偵測引擎
        self.wake_stream = None
        if self.whisper_model:
            try:
                from wake_stream_detector import WakeStreamDetector
                self.wake_stream = WakeStreamDetector(
                    whisper_model=self.whisper_model,
                    on_wake_callback=self._on_wake_stream_detected,
                    loop=asyncio.get_event_loop(),
                )
                print("✅ [Engine] P3 WakeStreamDetector 已就緒。", flush=True)
            except Exception as e:
                print(f"⚠️ [Engine Warning] WakeStreamDetector 載入失敗: {e}", flush=True)

    def start(self):
        """🚀 [Lifecycle] 啟動背景處理任務 (需在 Loop 啟動後呼叫)"""
        self.is_listening = True  # 🚀 [T-06 Fix] 確保啟動時旗標重置
        
        # 🛡️ [Idempotent Fix] 檢查現有任務，避免重複啟動看門狗
        if self._watchdog_task and not self._watchdog_task.done():
            print("🛡️ [VAD] 看門狗已在運作中，跳過重複啟動。", flush=True)
            return
            
        print("🚀 [Engine] 正在啟動背景 VAD 看門狗...", flush=True)
        self._watchdog_task = self.bot.loop.create_task(self._vad_watchdog())

    def stop(self):
        """🚀 [T-06 Fix] 優雅停止 VAD 看門狗，防止 /dismiss 後幽靈 Task 殘留"""
        self.is_listening = False
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            print("🛑 [Engine] VAD 看門狗已取消，幽靈任務已清除。", flush=True)

    async def _on_wake_stream_detected(self, user_id: int, first_audio_time: float, text: str) -> None:
        """P3 WakeStreamDetector 早期偵測到喚醒詞的回呼。"""
        speaker_name = f"User_{user_id}"
        for guild in self.bot.guilds:
            member = guild.get_member(user_id)
            if member:
                speaker_name = member.nick if member.nick else member.display_name
                break
        if self.stt_callback:
            await self.stt_callback(
                speaker_name, text, first_audio_time,
                b"",          # 早期偵測，無完整 WAV
                prosody_data=None,
                is_wake_check=True,
                track="A",    # 聲學層直接確認，等同 regex Track A
            )

    def get_active_sink(self) -> RealtimeVADSink:
        """🛡️ [Operation Sentinel] 獲取當前語音接收器以便監控心跳"""
        sink = None
        # 1. 優先從 Engine 內部緩存獲取 (由 summon/healing 直接注入)
        if hasattr(self, 'sink') and self.sink:
            sink = self.sink
        else:
            # 2. 遍歷 VoiceClients 嘗試探查
            if self.bot.voice_clients:
                vc = self.bot.voice_clients[0]
                # 🚀 [Sentinel 3.0] 強化追蹤：檢查多種可能的 Sink 儲存位置 (相容 0.5.2a+ 版本)
                sink_candidates = [
                    getattr(vc, 'sink', None),                          # VoiceRecvClient.sink 
                    getattr(getattr(vc, '_reader', None), 'sink', None), # VoiceReader.sink
                    getattr(getattr(vc, '_reader', None), '_sink', None) # VoiceReader._sink
                ]
                for s in sink_candidates:
                    if s and isinstance(s, RealtimeVADSink):
                        self.sink = s
                        sink = s
                        break
        
        # 🚀 [Linkage] 確保 Sink 能夠存取分析器與串流偵測器
        if sink and not sink.meta_analyzer:
            sink.meta_analyzer = self.meta_analyzer
        if sink and self.wake_stream and not sink.wake_stream:
            sink.wake_stream = self.wake_stream
        return sink

    async def _vad_watchdog(self):
        """
        🚀 [True RMS VAD Injection]
        背景看門狗：監控玩家是否停止說話 (基於真實 RMS 音量)
        此迴圈位於 Engine 層，確保即便在 Open Mic 雜訊下也能根據音量斷句。
        """
        print("🛡️ [VAD] True RMS 看門狗已啟動。", flush=True)
        loop_counter = 0
        while self.is_listening:
            await asyncio.sleep(0.5)
            loop_counter += 1
            if self.debug_vad_heartbeat and loop_counter % 20 == 0: # 每 10 秒印一次心跳
                print("💓 [Engine] VAD Watchdog 心跳正常，正在監控中...", flush=True)
                
            now = time.time()
            
            # 取得當前 Sink
            sink = self.get_active_sink()
            if not sink:
                continue
                
            # 🛡️ [Dynamic VAD] 依據對話熱度動態決定 STT 截斷閾值
            # 預設最低門檻改為 0.8s (Soft Threshold)，交由 Semantic ETD 判斷完整度
            stt_vad_threshold = self.conv_buffer.get_conversation_temperature() if hasattr(self, "conv_buffer") else 0.8
            
            for user_id in list(sink.user_buffers.keys()):
                # 🚀 [Logic Fix] 靜默判定邏輯：必須基於「真實人聲」而非「封包心跳」
                # 若尚未偵測到人聲 (User_Last_Spoken == 0)，則不執行靜默切割，僅累積緩衝
                last_spoken = sink.user_last_spoken_time.get(user_id, 0)
                
                # 情境 A: 偵測到靜默 (人聲消失超過 1.2s 且曾有過人聲)
                if last_spoken > 0 and (now - last_spoken > stt_vad_threshold):
                    buffer_bytes = len(sink.user_buffers[user_id])
                    # [VAD Relaxation] 從 96000 (0.5s) 下調至 19200 (0.1s)，容忍不穩定流
                    if buffer_bytes > 19200:
                        print(f"✂️ [VAD] 偵測到 {stt_vad_threshold}s 靜音 (User_{user_id})，聚合 {buffer_bytes} bytes 並送往 STT。", flush=True)
                        audio_data = bytes(sink.user_buffers[user_id])
                        sink.user_buffers[user_id] = bytearray()
                        sink.user_last_spoken_time[user_id] = 0 # 重置，等待下一段語音
                        
                        # 異步送往 STT
                        asyncio.create_task(
                            self.process_audio_slice(user_id, audio_data, last_spoken)
                        )
                    else:
                        # 緩衝區太短，視為雜訊
                        if buffer_bytes > 0:
                            print(f"🗑️ [VAD] 緩衝區過短 ({buffer_bytes} bytes)，判定為雜訊，捨棄中 (User_{user_id})。", flush=True)
                        sink.user_buffers[user_id] = bytearray()
                        sink.user_last_spoken_time[user_id] = 0
                        sink.user_wake_check_count.pop(user_id, None)
                        if sink.wake_stream:
                            sink.wake_stream.on_speech_end(user_id)

                # 情境 B: 說太長了，達到聚合上限 (例如 12 秒)
                first_audio = sink.user_first_audio_time.get(user_id, 0)
                if first_audio > 0 and (now - first_audio > self.MAX_AUDIO_CHUNK_DURATION):
                    buffer_bytes = len(sink.user_buffers[user_id])
                    if buffer_bytes > 19200:
                        print(f"⏲️ [VAD] 說太長了 ({self.MAX_AUDIO_CHUNK_DURATION}s)，User_{user_id} 聚合 {buffer_bytes} bytes 並強制送往 STT。", flush=True)
                        audio_data = bytes(sink.user_buffers[user_id])
                        sink.user_buffers[user_id] = bytearray()
                        # 注意：此處不重置 last_spoken，僅重置 first_audio，讓使用者能繼續說下去
                        sink.user_first_audio_time[user_id] = now 
                        
                        asyncio.create_task(
                            self.process_audio_slice(user_id, audio_data, first_audio)
                        )
                    else:
                        sink.user_buffers[user_id] = bytearray()
                        sink.user_first_audio_time[user_id] = 0
                
                # 情境 C: 保護機制 (防止記憶體膨脹)：若持續超過 10 秒都沒有達到靜默 (可能是雜訊過大)，強制觸發 Flush
                elif len(sink.user_buffers[user_id]) > 192000 * 10:
                    buffer_bytes = len(sink.user_buffers[user_id])
                    print(f"⚠️ [VAD] 緩衝區達到安全閾值 (User_{user_id})，執行強制 Flush 而非截斷。", flush=True)
                    audio_data = bytes(sink.user_buffers[user_id])
                    sink.user_buffers[user_id] = bytearray()
                    sink.user_last_spoken_time[user_id] = 0
                    
                    asyncio.create_task(
                        self.process_audio_slice(user_id, audio_data, now - 10)
                    )

            # 🚀 [Operation Social Awareness] 檢查靜音與補位觸發
            voice_controller = self.bot.get_cog("VoiceController")
            if voice_controller:
                # 🛡️ [RMS-based Silence Detection] 
                # 尋找全頻道最後一次「真實發聲」(RMS > THRESHOLD) 的時間，以排除 Open Mic 雜訊
                active_vocal_times = [t for t in sink.user_last_spoken_time.values()]
                last_user_speech = max(active_vocal_times) if active_vocal_times else sink.last_audio_packet_time
                
                # 🚀 [Marvin Awareness] 將馬文最後發言時間納入計算，確保其剛說完時不會立即觸發社交補位
                last_marvin_speech = getattr(voice_controller, "last_marvin_speech_time", 0)
                global_last_spoken = max(last_user_speech, last_marvin_speech)
                
                silence_duration = now - global_last_spoken
                
                if voice_controller.pending_intervention:
                    if silence_duration > voice_controller.current_vad_delay:
                        if time.time() < voice_controller.pending_intervention["expire_at"]:
                            vc = next((v for v in self.bot.voice_clients if v.is_connected()), None)
                            # 確保目前沒有在播放音樂或語音，且隊列已空
                            if not (vc and vc.is_playing()) and not voice_controller.is_playing_audio and voice_controller.tts_queue_duration == 0:
                                print(f"🎯 [社交補位] 尋獲完美靜音空檔 ({silence_duration:.1f}s)，開始插話！", flush=True)
                                self.bot.loop.create_task(voice_controller.play_intervention())
    async def clear_buffers(self):
        """🚀 [Chief Architect Action] 徹底清空所有待處理的語音與計時器"""
        print("🧹 [Engine] 正在執行 Phantom Purge，清空所有語音緩衝區與計時器...", flush=True)
        
        # 1. 取消所有正在等待 1.2s Debounce 的任務
        count = 0
        for user_id, timer in self.audio_timers.items():
            timer.cancel()
            count += 1
        self.audio_timers = {}
        
        # 2. 清空緩衝區
        self.audio_buffers = {}
        
        print(f"✅ [Engine] 已取消 {count} 個待處理計時器，緩衝區已歸零。", flush=True)


    def _handle_raw_speech_start(self, user_id):
        """底層 VAD 偵測到發言開始：立即透過回呼通知控制器進行中斷邏輯"""
        if self.speech_start_callback:
            # 獲取暱稱 (Operation Traceability)
            speaker_name = f"User_{user_id}"
            for guild in self.bot.guilds:
                member = guild.get_member(user_id)
                if member:
                    speaker_name = member.nick if member.nick else member.display_name
                    break
            self.speech_start_callback(speaker_name, user_id=user_id)

    async def process_audio_slice(self, user_id, raw_pcm, start_time, is_wake_check=False):
        """
        接收 VAD 切出的 PCM 片段。此層級不再進行額外的 1.2s Debounce，
        因為 VAD 已確保 1.2s 靜音才切片。在此進行音訊校正後直接進入 STT。
        """
        # 🚀 [Chief Architect Action] 立即進行 STT，不再進行二次 1.2s Debounce
        if user_id not in self.audio_buffers:
            self.audio_buffers[user_id] = {
                "pcm": bytearray(),
                "first_start": start_time
            }
            
        self.audio_buffers[user_id]["pcm"].extend(raw_pcm)
        
        # 檢查是否達到 12s 聚合上限，若達到則強制 Flush
        buffer_duration = len(self.audio_buffers[user_id]["pcm"]) / (48000 * 2 * 2) # Stereo 48k 16-bit
        if buffer_duration >= self.MAX_AUDIO_CHUNK_DURATION:
            print(f"⌛ [Engine] User_{user_id} 聚合已達上限 ({self.MAX_AUDIO_CHUNK_DURATION}s)，強制 Flush。", flush=True)

        # 🛡️ [Bug Fix P3] 移除無效 if/else（兩個分支都執行相同操作），直接 Flush
        await self._flush_audio_to_stt(user_id, is_wake_check=is_wake_check)

    async def _flush_audio_to_stt(self, user_id, is_wake_check=False):
        if user_id not in self.audio_buffers and not is_wake_check:
            return

        # 並發上限保護：wake_check 與 full-STT 使用獨立計數器，互不阻塞
        if is_wake_check:
            if self._wake_inflight >= self._MAX_WAKE_INFLIGHT:
                logger.warning(f"⚠️ [STT Drop] wake_inflight={self._wake_inflight} ≥ {self._MAX_WAKE_INFLIGHT}，丟棄本次 wake_check (User_{user_id})")
                return
        else:
            if self._full_stt_inflight >= self._MAX_FULL_STT_INFLIGHT:
                logger.warning(f"⚠️ [STT Drop] full_stt_inflight={self._full_stt_inflight} ≥ {self._MAX_FULL_STT_INFLIGHT}，丟棄本次 full-STT (User_{user_id})")
                return

        if is_wake_check:
            raw_pcm = bytes(self.audio_buffers[user_id]["pcm"]) if user_id in self.audio_buffers else b""
            start_time = self.audio_buffers[user_id]["first_start"] if user_id in self.audio_buffers else 0
        else:
            data = self.audio_buffers.pop(user_id)
            if user_id in self.audio_timers:
                del self.audio_timers[user_id]
            raw_pcm = bytes(data["pcm"])
            start_time = data["first_start"]

        if not raw_pcm:
            return

        if is_wake_check:
            self._wake_inflight += 1
        else:
            self._full_stt_inflight += 1
        wav_path = None
        print(f"🎬 [Engine] {'[WakeCheck]' if is_wake_check else ''} 開始處理聚合音訊 (User_{user_id}, Size: {len(raw_pcm)} bytes)", flush=True)
        try:
            pcm_array = np.frombuffer(raw_pcm, dtype=np.int16)
            rms = int(np.sqrt(np.mean(pcm_array.astype(np.float32)**2)))
            duration = len(raw_pcm) / (48000 * 2 * 2)

            # 🚀 [Operation Golden Ear] 自動增益補償 (Normalization)
            processed_pcm = raw_pcm
            if 100 < rms < 2500:
                print(f"🔊 [Golden Ear] 檢測到音量較小 (RMS: {rms})，自動執行 1.8x 增益補正...", flush=True)
                processed_pcm = (pcm_array.astype(np.float32) * 1.8).clip(-32768, 32767).astype(np.int16).tobytes()
                new_array = np.frombuffer(processed_pcm, dtype=np.int16)
                new_rms = int(np.sqrt(np.mean(new_array.astype(np.float32)**2)))
                print(f"📊 [Audio Audit] User_{user_id} | RMS: {rms} -> {new_rms} | 長度: {duration:.2f}s", flush=True)
            else:
                print(f"📊 [Audio Audit] User_{user_id} | RMS: {rms} | 長度: {duration:.2f}s", flush=True)

            # 每次呼叫都用 time_ns() 確保唯一路徑，避免同秒的 wake_check 與 full-STT 衝突
            wav_path = os.path.abspath(f"tmp_stt_{user_id}_{time.time_ns()}.wav")
            with wave.open(wav_path, 'wb') as wav_file:
                wav_file.setnchannels(2)
                wav_file.setsampwidth(2)
                wav_file.setframerate(48000)
                wav_file.writeframes(processed_pcm)

            with open(wav_path, 'rb') as f:
                wav_bytes = f.read()

            # 預先轉換為 mono 16kHz float32，讓 Whisper 直接吃 array，不依賴磁碟上的檔案
            # 48kHz stereo int16 → 16kHz mono float32（downsample 3:1）
            _arr = np.frombuffer(processed_pcm, dtype=np.int16).reshape(-1, 2)
            whisper_audio = _arr.mean(axis=1)[::3].astype(np.float32) / 32768.0

            # 解析暱稱
            speaker_name = f"User_{user_id}"
            for guild in self.bot.guilds:
                member = guild.get_member(user_id)
                if member:
                    speaker_name = member.nick if member.nick else member.display_name
                    break

            prosody_data = self.meta_analyzer.calculate_prosody(user_id, None, duration)
            await self._process_stt_hybrid(speaker_name, wav_path, wav_bytes, start_time,
                                           prosody_data=prosody_data, is_wake_check=is_wake_check,
                                           whisper_audio=whisper_audio, user_id=user_id)

        except Exception as e:
            print(f"[Engine Error] Audio flush failed: {e}")
        finally:
            if is_wake_check:
                self._wake_inflight -= 1
            else:
                self._full_stt_inflight -= 1
            # 統一在 flush 結束後清理暫存檔，避免 Whisper thread cancel 後仍讀到已刪除的檔
            if wav_path and os.path.exists(wav_path):
                try:
                    os.remove(wav_path)
                except OSError:
                    pass

    # ── P2: STT 引擎拆解為獨立協程 ─────────────────────────────────────────────

    async def _run_swift_stt(self, wav_path: str, is_wake_check: bool) -> str:
        """執行 macOS Swift STT，回傳辨識文字或空字串。"""
        process = None
        try:
            env = os.environ.copy()
            base_context = "Marvin,馬文,碼文,麻文,艾馬文,馬問,馬門,嗨馬文,Hi Marvin"
            if hasattr(self.bot.router, 'game_dict_string') and self.bot.router.game_dict_string:
                env["STT_CONTEXT_STRINGS"] = f"{base_context},{self.bot.router.game_dict_string}"
            else:
                env["STT_CONTEXT_STRINGS"] = base_context
            stt_args = ["./macos_stt_bin", wav_path]
            if is_wake_check:
                stt_args.append("--wake-check")
            process = await asyncio.create_subprocess_exec(
                *stt_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10.0)
            if process.returncode == 0:
                for line in stdout.decode("utf-8").splitlines():
                    line = line.strip()
                    if line and not any(line.startswith(p) for p in ("🔍", "✅", "❌", "DEBUG:", "📚")):
                        return line
            else:
                logger.warning(f"[Swift STT] 執行失敗 (Code: {process.returncode})")
        except asyncio.TimeoutError:
            logger.warning("[Swift STT] 10s 超時，強制終止 macos_stt_bin")
            if process is not None:
                try:
                    process.kill()
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"[Swift STT] Exception: {e}")
        return ""

    async def _run_mlx_whisper_stt(self, wav_path: str) -> str:
        """執行 MLX Whisper STT（subprocess），回傳辨識文字或空字串。

        以 subprocess 執行 mlx_whisper_bin.py，超時時 process.kill() 真正終止，
        不產生 zombie threads（對比 asyncio.to_thread 無法殺 OS thread）。
        尚未接入 _process_stt_hybrid — 基礎設施備用。
        """
        process = None
        try:
            mlx_model = os.getenv("MLX_WHISPER_MODEL", "mlx-community/whisper-base-mlx-8bit")
            import sys as _sys
            process = await asyncio.create_subprocess_exec(
                _sys.executable, "./mlx_whisper_bin.py", wav_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "MLX_WHISPER_MODEL": mlx_model},
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15.0)
            if process.returncode == 0:
                return stdout.decode("utf-8").strip()
            else:
                logger.warning(f"[MLX Whisper] 執行失敗 (Code: {process.returncode})")
        except asyncio.TimeoutError:
            logger.warning("[MLX Whisper] 15s 超時，強制終止")
            if process is not None:
                try:
                    process.kill()
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"[MLX Whisper] Exception: {e}")
        return ""

    async def _run_whisper_stt(self, audio) -> str:
        """執行 Faster-Whisper STT，回傳辨識文字或空字串。
        audio 可為 numpy float32 array（優先）或 WAV 檔路徑（fallback）。

        Zombie-thread 防護：使用 threading.Semaphore(1)，由 thread 自身在 finally 釋放。
        asyncio 的 wait_for timeout 只取消 Future，不殺 thread；semaphore 確保
        前一個 thread 仍在跑時新呼叫直接 drop，最多同時只有 1 條 Whisper thread 存活。
        """
        if not self.whisper_model:
            return ""

        # Drop immediately if previous thread is still running
        if not self._whisper_thread_sem.acquire(blocking=False):
            logger.warning("[Whisper STT] 前一次辨識仍在執行，跳過（zombie guard）")
            return ""

        whisper_prompt = "Marvin, Hi Marvin, 馬文, 艾馬文, 艾瑪文, 幫忙, 玩家對話。"
        active_dict = getattr(self.bot.router, 'game_dict_string', "")
        if active_dict:
            whisper_prompt += f", {active_dict}"

        _model = self.whisper_model
        _prompt = whisper_prompt
        _sem = self._whisper_thread_sem

        # faster-whisper.transcribe() 回傳 lazy generator，必須在 thread 內完整 iterate
        def _transcribe_eager():
            try:
                _t0 = time.monotonic()
                segs, _ = _model.transcribe(
                    audio,
                    beam_size=1,
                    language="zh",
                    initial_prompt=_prompt,
                    vad_filter=True,
                    vad_parameters=dict(min_silence_duration_ms=500),
                )
                result = "".join(s.text for s in segs).strip()
                logger.debug(f"[Whisper STT] 推理耗時 {time.monotonic()-_t0:.2f}s")
                return result
            finally:
                # Release only when thread actually finishes — not when asyncio cancels
                _sem.release()

        try:
            loop = asyncio.get_event_loop()
            text = await asyncio.wait_for(
                loop.run_in_executor(self._whisper_executor, _transcribe_eager),
                timeout=30.0,
            )
            if text and is_whisper_hallucination(text, _prompt):
                logger.warning(f"[Whisper STT] 幻覺偵測，丟棄輸出: '{text[:60]}'")
                return ""
            return text or ""
        except asyncio.TimeoutError:
            logger.warning("[Whisper STT] 30s 超時，thread 仍在跑（semaphore 由 thread 釋放）")
        except Exception as e:
            logger.warning(f"[Whisper STT] Exception: {e}")
        return ""

    async def _process_stt_hybrid(self, speaker_name, wav_path, wav_bytes, timestamp, prosody_data: dict = None, is_wake_check=False, whisper_audio=None, user_id: int | None = None):
        """
        混合型 STT 處理器。
        Wake check 模式（P2）：Swift on-device + Whisper 並行競速，先到先得。
        完整句模式：Swift server → Whisper 序列備援（優先準確度）。
        Wake check 使用獨立 wake_stt_lock，不被同時進行的 full-utterance STT 阻塞。
        """
        _lock = self.wake_stt_lock if is_wake_check else self.stt_lock
        _lock_timeout = 12.0 if is_wake_check else 45.0
        try:
            await asyncio.wait_for(_lock.acquire(), timeout=_lock_timeout)
        except asyncio.TimeoutError:
            label = "wake_stt_lock" if is_wake_check else "stt_lock"
            logger.error(f"[STT Lock] {label} 等待超過 {_lock_timeout}s，放棄 (Speaker: {speaker_name})。lock 可能卡住，請確認。")
            return
        try:
            raw_text = ""
            used_engine = "None"

            # whisper_audio 為預先轉換的 mono 16kHz float32 array（由 _flush_audio_to_stt 提供）
            # 傳入 array 讓 Whisper 不依賴磁碟檔案，避免 cancel 後 thread 讀到已刪除的 wav
            _whisper_input = whisper_audio if whisper_audio is not None else wav_path

            _is_apple_platform = self.stt_engine in ("macos", "mlx")

            if is_wake_check:
                if _is_apple_platform:
                    # Apple platform: Swift-only wake_check — no Whisper to prevent zombie threads
                    print(f"🎙️ [Engine] [WakeCheck] Swift only (Speaker: {speaker_name})...", flush=True)
                    raw_text = await self._run_swift_stt(wav_path, is_wake_check=True)
                    if raw_text:
                        used_engine = "Swift"
                else:
                    # Linux: P2 race — Swift + Whisper parallel, first non-empty wins
                    print(f"🎙️ [Engine] [WakeCheck] Swift ⚡ Whisper 並行競速 (Speaker: {speaker_name})...", flush=True)
                    swift_t = asyncio.create_task(self._run_swift_stt(wav_path, is_wake_check=True))
                    whisper_t = asyncio.create_task(self._run_whisper_stt(_whisper_input))
                    name_map = {id(swift_t): "Swift", id(whisper_t): "Whisper"}
                    pending = {swift_t, whisper_t}
                    while pending and not raw_text:
                        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                        for t in done:
                            try:
                                text = t.result()
                                if text:
                                    raw_text = text
                                    used_engine = name_map[id(t)]
                                    for p in pending:
                                        p.cancel()
                                    break
                            except Exception:
                                pass
                if raw_text:
                    print(f"✅ [STT Output] {speaker_name}: {raw_text} (Engine: {used_engine})", flush=True)
                    # Wake-check 結果幻覺過濾
                    _wake_hal_prompt = "嗨馬文,Hi Marvin,Marvin,馬文,艾馬文"
                    if is_whisper_hallucination(raw_text, _wake_hal_prompt):
                        logger.warning(f"[WakeCheck] 幻覺丟棄 ({used_engine}): '{raw_text[:80]}'")
                        raw_text = ""
                        if user_id is not None and hasattr(self, 'sink') and self.sink:
                            self.sink.elevate_vad(user_id, duration=15.0)
            else:
                # 序列備援：Swift server 優先（最高準確度），失敗才用 Whisper
                print(f"🎙️ [Engine] 啟動 macOS Native Swift STT (Speaker: {speaker_name})...", flush=True)
                raw_text = await self._run_swift_stt(wav_path, is_wake_check=False)
                if raw_text:
                    used_engine = "Swift"
                    print(f"✅ [STT Output] {speaker_name}: {raw_text} (Engine: Swift)", flush=True)
                # Apple platform: Swift 為主引擎，不使用 Whisper 備援（避免 zombie thread 累積）
                # Linux: Whisper 是唯一引擎，維持原行為
                if not raw_text and self.whisper_model and not _is_apple_platform:
                    print(f"🎙️ [Engine] 啟動備援 Faster-Whisper 辨識 (Speaker: {speaker_name})...", flush=True)
                    raw_text = await self._run_whisper_stt(_whisper_input)
                    if raw_text:
                        used_engine = "Whisper"
                        print(f"✅ [STT Output] {speaker_name}: {raw_text} (Engine: Whisper)", flush=True)

        finally:
            _lock.release()

        # 3. 最終結果判定
        if not raw_text:
            if used_engine == "None":
                print(f"❌ [STT Fatal] 所有辨識方案皆失敗 (Speaker: {speaker_name})", flush=True)
            else:
                # 這種情況其實不會發生，因為 used_engine 只有在 text 有值時才會被設定
                pass

        # 3. 回傳結果
        if self.stt_callback and raw_text:
            # --- [Track A] Immediate Regex Path ---
            # 讓 "馬文" 這種句首喚醒詞不用等到 LLM 清洗完才被識別 (0ms 延遲)
            filter_res = pre_filter_speech(raw_text)
            action_A = filter_res.get("action")
            is_wake_A = action_A in ["fast_intervene", "force_intervene"]
            
            if is_wake_A:
                logger.info(f"⚡ [Track A] Regex Hit! Immediate wake triggering for '{raw_text}'...")
                # 立即觸發回調 (使用原始文字，並標記 Track A)
                await self.stt_callback(speaker_name, raw_text, timestamp, wav_bytes, prosody_data=prosody_data, is_wake_check=is_wake_check, track="A")
                # 如果只是喚醒檢查 (1.8s Snapshot)，任務已達成，提早退出
                if is_wake_check:
                    return

            # --- [Track B] LLM Clean & Fallback Path ---
            cleaned_text = raw_text
            is_wake_B = False
            
            if hasattr(self.bot, 'router') and hasattr(self.bot.router, 'clean_stt_text'):
                # Phase 2: 計算對話脈絡訊號
                _now = time.time()
                _recent_10 = self.conv_buffer.get_last_n_utterances(10)
                # context_active: Marvin 在 90s 內說過話（對話進行中）
                context_active = any(
                    e["speaker"] == "Marvin" and (_now - e["timestamp"]) <= 90.0
                    for e in _recent_10
                )
                # marvin_just_spoke: Marvin 在 15s 內剛結束說話 → 使用者最可能此時呼叫
                marvin_just_spoke = any(
                    e["speaker"] == "Marvin" and (_now - e["timestamp"]) <= 15.0
                    for e in _recent_10
                )
                recent_ctx = self.conv_buffer.get_last_n_utterances(5)
                clean_res = await self.bot.router.clean_stt_text(
                    raw_text, context=recent_ctx,
                    speaker=speaker_name, context_active=context_active,
                    marvin_just_spoke=marvin_just_spoke
                )
                cleaned_text = clean_res["text"]
                is_wake_B = clean_res["is_wake"]

                # Phase 3: fire speculative prefetch on high-confidence wake
                _wi = clean_res.get("wake_intent") or 0.0
                if _wi >= 0.85 and is_wake_B:
                    _router = getattr(getattr(self, 'bot', None), 'router', None)
                    if _router and hasattr(_router, '_speculative_response'):
                        _ph = self.conv_buffer.get_last_n_utterances(5)
                        _router._pending_prefetch[speaker_name] = asyncio.create_task(
                            _router._speculative_response(speaker_name, cleaned_text, _ph)
                        )
                        logger.info(f"🚀 [Speculative] Prefetch started for {speaker_name} (intent={_wi:.2f})")

            # 🚀 [Prosody] 最終計算精確的 WPS (基於識別後的文字)
            if prosody_data:
                _text_for_wps = cleaned_text.replace(" ", "")
                prosody_data["char_count"] = len(_text_for_wps)
                prosody_data["wps"] = round(prosody_data["char_count"] / prosody_data["physical_duration"], 2) if prosody_data["physical_duration"] > 0 else 0
            
            # --- 決定是否再次觸發 STT Callback ---
            # 1. 如果 Track A 沒中，但 Track B 中了 (補漏喚醒)
            # 2. 如果這不是喚醒檢查 (正常發言/指令，需要 cleaned_text 進記憶體)
            should_callback = False
            if is_wake_B and not is_wake_A:
                logger.info(f"✨ [Track B] Cleaned Hit! Fallback wake for '{cleaned_text}'...")
                should_callback = True
            elif not is_wake_check:
                # 正常發言流程，不論 Track A 有沒有中，我們最後都要把 cleaned_text 送回 Controller
                # 🚀 [Optimization] 僅針對有實質內容 (長度 > 3) 的語句進行清洗後的回調與紀錄
                if len(cleaned_text.strip()) > 3:
                     should_callback = True
                else:
                     logger.debug(f"⏭️ [Track B] 語句太短 ({cleaned_text})，跳過後端同步。")

            if should_callback:
                # 觸發回調 (標記 Track B)，同時傳入喚醒信心值供 TTS 決策
                _b_wake_intent = clean_res.get("wake_intent") if isinstance(clean_res, dict) else None
                await self.stt_callback(speaker_name, cleaned_text, timestamp, wav_bytes, prosody_data=prosody_data, is_wake_check=is_wake_check, track="B", wake_intent=_b_wake_intent)

                # Phase 2 false-wake proxy: if harvest is empty 1.1s after a Track B wake,
                # that's a likely false wake — feed signal back to WakeSignalFusion
                if is_wake_B and not is_wake_A:
                    _ts = timestamp
                    _spk = speaker_name
                    async def _check_false_wake():
                        await asyncio.sleep(1.1)
                        harvest = self.conv_buffer.get_harvest(_ts, before=3.0, after=1.0)
                        if len(harvest.strip()) < 5:
                            fusion = getattr(getattr(self, 'bot', None), 'router', None)
                            fusion = getattr(fusion, 'wake_fusion', None) if fusion else None
                            if fusion:
                                fusion.record_outcome(_spk, False)
                                logger.info(f"📊 [FusionFeedback] Empty harvest → false wake recorded for {_spk}")
                    asyncio.create_task(_check_false_wake())
        elif not raw_text:
            print(f"🔇 [Engine] {speaker_name} 辨識完畢，但無文字內容。", flush=True)

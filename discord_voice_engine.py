import discord
from discord.ext import commands
import asyncio
import concurrent.futures
import json
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
from quality_metrics import record_metric
import pipeline_timing

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

    # DAVE E2EE: 若 guild 啟用 end-to-end encryption, SRTP plaintext 內層還是 davey ciphertext.
    # voice_state.dave_ready 由 discord.py 2.7.x 內建 MLS handshake 維持; davey 套件提供 decrypt.
    try:
        import davey as _davey
        _davey_media_audio = _davey.MediaType.audio
    except Exception:
        _davey = None
        _davey_media_audio = None

    def _maybe_dave_decrypt(packet, plaintext: bytes) -> bytes:
        state = getattr(voice_client, "_connection", None)
        if state is None or _davey is None:
            return plaintext
        if not getattr(state, "dave_ready", False):
            return plaintext
        uid = voice_client._ssrc_to_id.get(packet.ssrc)
        if uid is None:
            return plaintext
        try:
            return state.dave_session.decrypt(uid, _davey_media_audio, plaintext)
        except Exception as _e:
            logger.debug(f"[DAVE] decrypt fallback uid={uid}: {_e}")
            return plaintext

    def _synced_decrypt_rtp(packet):
        try:
            return _maybe_dave_decrypt(packet, orig_rtp(packet))
        except _CryptoError:
            try:
                new_key = bytes(voice_client.secret_key)
                decryptor.update_secret_key(new_key)
                logger.info("[KeySync] RTP CryptoError → reader secret_key 已同步")
                return _maybe_dave_decrypt(packet, orig_rtp(packet))
            except _CryptoError:
                # 重抓 key 後仍 CryptoError：真的解不開（少見、transient）。原樣上拋，
                # reader.py 的 except CryptoError 分支會單行 log + 乾淨 drop（無 traceback）。
                logger.debug("[KeySync] RTP 重試仍 CryptoError，drop 此封包")
                raise
            except Exception as _e:
                # 重試炸非 CryptoError（RTCP/unknown-ssrc 雜散封包算出負 buffer 長度等）。
                # 這不是 key 問題、重試無意義；轉成 CryptoError 讓 reader.py 走乾淨 drop 分支，
                # 避免 except Exception: log.exception 噴整段 traceback（原 289/天噪音來源）。
                logger.debug(f"[KeySync] RTP 封包無法解密（非 key 問題），drop: {_e}")
                raise _CryptoError("malformed packet dropped") from None

    def _synced_decrypt_rtcp(packet_data):
        try:
            return orig_rtcp(packet_data)
        except _CryptoError:
            try:
                new_key = bytes(voice_client.secret_key)
                decryptor.update_secret_key(new_key)
                logger.info("[KeySync] RTCP CryptoError → reader secret_key 已同步")
                return orig_rtcp(packet_data)
            except _CryptoError:
                logger.debug("[KeySync] RTCP 重試仍 CryptoError，drop 此封包")
                raise
            except Exception as _e:
                # 同 RTP：非 key 問題的 malformed 封包轉 CryptoError，避免 library 噴 traceback
                logger.debug(f"[KeySync] RTCP 封包無法解密（非 key 問題），drop: {_e}")
                raise _CryptoError("malformed packet dropped") from None

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
        # 🎮 遊戲 cog 進入 game_mode 時可設此值，將靜態閾值臨時拉高
        # （避免遊戲中 cough/敲鍵盤/小聲閒聊觸發 STT；不影響 wake-word 模式）
        self.game_mode_rms_bump = 0
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

        # production 建構於 bot 的 running loop 內 → 直接抓；
        # 非 async 情境（測試 / pytest-asyncio 已清掉 current loop）→ fallback，
        # 不依賴 current loop 存在（3.12 下 get_event_loop() 會 raise）。
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = asyncio.new_event_loop()
        # self.harvester_task = self.loop.create_task(self._harvester_loop()) # 🚀 [Watchdog] 準備搬遷至 Engine
        self.packet_count = 0
        self.last_audio_packet_time = time.time() # 🛡️ [Heartbeat]
        self.last_decrypted_audio_time = time.time() # 🛡️ [Operation Sentinel] 僅紀錄解密成功的時間點
        self.last_dave_error_time = 0.0 # 🛡️ [Sentinel] DAVE 失敗上報 throttle（每 5s 最多一次）
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
                            if self.sink_error_callback and (now - self.last_dave_error_time > 5):
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
                # DAVE 此時可能 ready 也可能 passthrough；只 log 「收到第一筆有效封包」
                # 這個事實，不誤稱「DAVE 解密成功」
                _dave_state = "DAVE+" if (dave_session and dave_session.ready) else "passthrough"
                print(f"🚀 [Sink] 捕捉第一筆有效語音 ({_dave_state}) 來源: {user.name}", flush=True)

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
            # 遊戲模式 bump：把靜態 floor 提高，過濾雜訊（cough、鍵盤、遠端閒聊）
            effective_static = self.RMS_THRESHOLD + self.game_mode_rms_bump
            dynamic_threshold = max(effective_static, noise_floor + delta_threshold)
            
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
            # 刻意不呼叫 sink_error_callback：partial/lost packet（opus decode 失敗
            # 等）通常是網路抖動，不是 DAVE 金鑰失效；上報會污染 Sentinel 計數器
            # 誤觸發 soft_repair。Sentinel 只應該被真正的 DAVE 解密失敗觸發
            # （已在上面 DAVE handling 區塊內處理）。
            if self.packet_count % 50 == 0:
                print(f"⚠️ [Sink.write Warning] {e}", flush=True)

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

        # Per-speaker language memory: updated after each successful transcription.
        # First utterance from an unknown speaker defaults to "zh".
        self._speaker_lang: dict[str, str] = {}

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
                            # 確保目前沒有在播放音樂或語音，且隊列已空。
                            # 🎛️ [Plan 12] flag=on：always-on mixer 讓 vc.is_playing() 永遠 True，
                            # 改靠 mixer 維護的 is_playing_audio（含音樂層）+ tts_queue_duration 判 idle。
                            _audio_busy = voice_controller.is_playing_audio or voice_controller.tts_queue_duration > 0
                            if getattr(voice_controller, "_plan12", False):
                                _idle = not _audio_busy
                            else:
                                _idle = not (vc and vc.is_playing()) and not _audio_busy
                            if _idle:
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
        # ContextVar propagates into create_task descendants automatically.
        pipeline_timing.start()
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
            # 遊戲狀態：非搶答者/非猜題者語音直接丟棄，不佔 full-STT inflight 名額
            _cogs = self.bot.cogs if hasattr(self.bot, "cogs") else None
            if _cogs is not None:
                for _cog_name in ("Busted99Cog", "BustedCog", "TurtleSoupCog"):
                    _game_cog = _cogs.get(_cog_name)
                    if _game_cog is not None and hasattr(_game_cog, "should_suppress_for_game_by_id"):
                        if _game_cog.should_suppress_for_game_by_id(user_id):
                            logger.debug(
                                "[Engine] game suppress (%s): user_id=%d 非參與者，跳過 full-STT dispatch",
                                _cog_name, user_id,
                            )
                            return
            self._full_stt_inflight += 1

        # 2026-05-20: idempotent inflight 釋放 closure。_process_stt_hybrid 在 STT
        # 完成（cleaner LLM 之前）就呼叫它，讓 cleaner 在 slot 外跑，避免 Groq 429
        # 慢 cleaner 佔住 STT 名額餓死其他 wake_check。finally 兜底（STT 早退時補釋放）。
        _inflight_released = False
        def _release_inflight():
            nonlocal _inflight_released
            if _inflight_released:
                return
            _inflight_released = True
            if is_wake_check:
                self._wake_inflight -= 1
            else:
                self._full_stt_inflight -= 1

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
                                           whisper_audio=whisper_audio, user_id=user_id,
                                           release_inflight=_release_inflight)

        except Exception as e:
            print(f"[Engine Error] Audio flush failed: {e}")
        finally:
            _release_inflight()
            # 統一在 flush 結束後清理暫存檔，避免 Whisper thread cancel 後仍讀到已刪除的檔
            if wav_path and os.path.exists(wav_path):
                try:
                    os.remove(wav_path)
                except OSError:
                    pass

    # ── Speaker language helpers ────────────────────────────────────────────────

    @staticmethod
    def _detect_text_lang(text: str) -> str:
        """Return 'en' if text is primarily Latin, else 'zh'."""
        if not text:
            return "zh"
        latin = sum(1 for c in text if "a" <= c.lower() <= "z")
        cjk = sum(1 for c in text if "一" <= c <= "鿿")
        return "en" if latin > cjk * 2 else "zh"

    def _get_speaker_lang(self, speaker: str) -> str:
        # 預設鎖定 zh — 全員繁中。auto-detect 已關（Whisper hallucination 會把 zh 玩家
        # 漂到 en，下次 STT 給 en hint 後更容易繼續 hallucinate，正反饋迴圈無法回頭）。
        # 若未來要支援多語系，改用 user-level config（Discord ID → lang），不要靠 STT 輸出推測。
        if os.environ.get("STT_AUTO_DETECT_LANG", "false").lower() == "true":
            return self._speaker_lang.get(speaker, "zh")
        return "zh"

    def _update_speaker_lang(self, speaker: str, text: str) -> None:
        # auto-detect 關閉時不更新（避免 hallucination 污染）
        if os.environ.get("STT_AUTO_DETECT_LANG", "false").lower() != "true":
            return
        if text:
            self._speaker_lang[speaker] = self._detect_text_lang(text)

    def _is_nan_speaker(self, user_id: int | None) -> bool:
        """此 user 是否走台語雲端 STT（雅婷）。allowlist 從 env NAN_SPEAKER_IDS（逗號分隔
        Discord user_id）讀，預設空 → 沒人走雅婷。依 user_id 而非顯示名（名稱會變/撞名）。"""
        if user_id is None:
            return False
        raw = os.getenv("NAN_SPEAKER_IDS", "")
        if not raw:
            return False
        return str(user_id) in {x.strip() for x in raw.split(",") if x.strip()}

    _LANG_TO_LOCALE = {"zh": "zh-TW", "en": "en-US"}

    # ── P2: STT 引擎拆解為獨立協程 ─────────────────────────────────────────────

    async def _run_swift_stt(self, wav_path: str, is_wake_check: bool, locale: str = "zh-TW") -> tuple[str, dict]:
        """執行 macOS Swift STT，回傳 (辨識文字, meta dict)。

        meta 包含 Swift 端送的聲學/韻律訊號（avg_confidence / min_confidence /
        avg_pause_duration / speaking_rate），供 J1 信心校準與 VAD 溫度判斷之後使用。
        """
        process = None
        meta: dict = {}
        try:
            env = os.environ.copy()
            # base_context: wake word 變體（從 stt_corrections.jsonl 萃取 Top wake 聽錯）
            # Siri/阿公/瑪利歐 是實測 frequency ≥2 的真實 STT 聲學混淆，加進來偏回「馬文」
            base_context = "Marvin,馬文,碼文,麻文,艾馬文,馬問,馬門,嗨馬文,Hi Marvin,Siri,阿公,瑪利歐"
            if hasattr(self.bot.router, 'game_dict_string') and self.bot.router.game_dict_string:
                env["STT_CONTEXT_STRINGS"] = f"{base_context},{self.bot.router.game_dict_string}"
            else:
                env["STT_CONTEXT_STRINGS"] = base_context
            env["STT_LOCALE"] = locale
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
                text = ""
                for line in stdout.decode("utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("__META__ "):
                        try:
                            meta = json.loads(line[len("__META__ "):])
                        except json.JSONDecodeError:
                            pass
                        continue
                    if any(line.startswith(p) for p in ("🔍", "✅", "❌", "DEBUG:", "📚")):
                        continue
                    text = line
                if text:
                    return text, meta
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
        return "", {}

    async def _run_groq_whisper_stt(self, wav_path: str, language: str = "zh") -> tuple[str, dict]:
        """Groq Whisper API STT，回傳 (辨識文字, meta dict)。meta 永遠為空 dict。

        使用 whisper-large-v3-turbo（速度快，準確度夠），免費額度 28,800s/day。
        在 asyncio.to_thread 內執行阻塞的 HTTP 上傳，不阻塞 event loop。
        """
        groq_key = os.getenv("GROQ_API_KEY", "")
        if not groq_key:
            logger.warning("[Groq Whisper] GROQ_API_KEY 未設定，跳過")
            return "", {}

        _lang = language
        def _upload():
            try:
                from groq import Groq
                client = Groq(api_key=groq_key)
                with open(wav_path, "rb") as f:
                    resp = client.audio.transcriptions.create(
                        model="whisper-large-v3-turbo",
                        file=("audio.wav", f, "audio/wav"),
                        language=_lang,
                        prompt="Marvin, 馬文, 艾馬文, Hi Marvin",
                    )
                return resp.text.strip()
            except Exception as e:
                logger.warning(f"[Groq Whisper] 上傳失敗: {e}")
                return ""

        try:
            text = await asyncio.wait_for(
                asyncio.to_thread(_upload),
                timeout=20.0,
            )
            if text and is_whisper_hallucination(text, "Marvin, 馬文, 艾馬文, Hi Marvin"):
                logger.warning(f"[Groq Whisper] 幻覺偵測，丟棄: '{text[:60]}'")
                return "", {}
            return text, {}
        except asyncio.TimeoutError:
            logger.warning("[Groq Whisper] 20s 超時")
        except Exception as e:
            logger.warning(f"[Groq Whisper] Exception: {e}")
        return "", {}

    async def _run_yating_stt(self, audio) -> tuple[str, dict]:
        """台語講者雲端 STT lane（雅婷 asr-zh-tw-std，輸出已正規化成華語漢字）。

        audio 為 16kHz mono float32 array（即 _process_stt_hybrid 的 whisper_audio）。
        缺金鑰 / 缺音訊 / 網路失敗 / 逾時 → 一律回 ("",{})，讓 caller 降級回 Swift。
        """
        api_key = os.getenv("YATING_API_KEY", "")
        if not api_key or audio is None:
            return "", {}
        try:
            import yating_stt
            pcm = yating_stt.pcm16_from_float(audio)
            if not pcm:
                return "", {}
            text = await yating_stt.transcribe(api_key, pcm, timeout=8.0)
            return (text, {}) if text else ("", {})
        except Exception as e:
            logger.warning(f"[Yating] {type(e).__name__}: {e}，降級回 Swift")
            return "", {}

    async def _run_whisper_stt(self, audio, language: str = "zh") -> tuple[str, dict]:
        """執行 Faster-Whisper STT，回傳 (辨識文字, meta dict)。meta 永遠為空 dict。
        audio 可為 numpy float32 array（優先）或 WAV 檔路徑（fallback）。

        Zombie-thread 防護：使用 threading.Semaphore(1)，由 thread 自身在 finally 釋放。
        asyncio 的 wait_for timeout 只取消 Future，不殺 thread；semaphore 確保
        前一個 thread 仍在跑時新呼叫直接 drop，最多同時只有 1 條 Whisper thread 存活。
        """
        if not self.whisper_model:
            return "", {}

        # Drop immediately if previous thread is still running
        if not self._whisper_thread_sem.acquire(blocking=False):
            logger.warning("[Whisper STT] 前一次辨識仍在執行，跳過（zombie guard）")
            return "", {}

        whisper_prompt = "Marvin, Hi Marvin, 馬文, 艾馬文, 艾瑪文, 幫忙, 玩家對話。"
        active_dict = getattr(self.bot.router, 'game_dict_string', "")
        if active_dict:
            whisper_prompt += f", {active_dict}"

        _model = self.whisper_model
        _prompt = whisper_prompt
        _sem = self._whisper_thread_sem
        _lang = language

        # faster-whisper.transcribe() 回傳 lazy generator，必須在 thread 內完整 iterate
        def _transcribe_eager():
            try:
                _t0 = time.monotonic()
                segs, _ = _model.transcribe(
                    audio,
                    beam_size=1,
                    language=_lang,
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
                return "", {}
            return (text or ""), {}
        except asyncio.TimeoutError:
            logger.warning("[Whisper STT] 30s 超時，thread 仍在跑（semaphore 由 thread 釋放）")
        except Exception as e:
            logger.warning(f"[Whisper STT] Exception: {e}")
        return "", {}

    async def _process_stt_hybrid(self, speaker_name, wav_path, wav_bytes, timestamp, prosody_data: dict = None, is_wake_check=False, whisper_audio=None, user_id: int | None = None, release_inflight=None):
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
            pipeline_timing.mark("stt_start")
            raw_text = ""
            used_engine = "None"
            stt_meta: dict = {}

            # whisper_audio 為預先轉換的 mono 16kHz float32 array（由 _flush_audio_to_stt 提供）
            # 傳入 array 讓 Whisper 不依賴磁碟檔案，避免 cancel 後 thread 讀到已刪除的 wav
            _whisper_input = whisper_audio if whisper_audio is not None else wav_path

            _is_apple_platform = self.stt_engine in ("macos", "mlx")

            # Per-speaker language: use previous utterance's detected language as STT hint.
            # First utterance from an unknown speaker defaults to "zh".
            _sp_lang = self._get_speaker_lang(speaker_name)
            _sp_locale = self._LANG_TO_LOCALE.get(_sp_lang, "zh-TW")

            # 2026-05-20: STT_SWIFT_STRICT=true 完整關掉 Groq Whisper fallback
            # （原本只擋正常語音路徑，wake check 仍走 Groq → 噪音時幻覺如「李宗盛」）
            _swift_strict = os.getenv("STT_SWIFT_STRICT", "").lower() in ("1", "true", "yes")

            if is_wake_check:
                if _is_apple_platform:
                    # Apple platform: Swift first; Groq HTTP fallback when Swift returns empty.
                    # Whisper is still excluded here to prevent zombie threads (7cbc32e).
                    # 5/18 20:28 incident: 35× Swift EDEADLK under macOS memory pressure
                    # left wake pipeline completely silent because there was no fallback.
                    # 5/20 incident: Groq Whisper 在低訊號 wake check 上幻覺「李宗盛」等
                    # 內容 → STT_SWIFT_STRICT=true 完整關掉 Groq fallback。
                    print(f"🎙️ [Engine] [WakeCheck] Swift (Speaker: {speaker_name})...", flush=True)
                    raw_text, stt_meta = await self._run_swift_stt(wav_path, is_wake_check=True, locale=_sp_locale)
                    if raw_text:
                        used_engine = "Swift"
                    elif os.getenv("GROQ_API_KEY") and not _swift_strict:
                        print(f"🎙️ [Engine] [WakeCheck] Swift empty → Groq fallback (Speaker: {speaker_name})...", flush=True)
                        raw_text, stt_meta = await self._run_groq_whisper_stt(wav_path, language=_sp_lang)
                        if raw_text:
                            used_engine = "Groq"
                else:
                    # Linux: P2 race — Swift + Whisper parallel, first non-empty wins
                    print(f"🎙️ [Engine] [WakeCheck] Swift ⚡ Whisper 並行競速 (Speaker: {speaker_name})...", flush=True)
                    swift_t = asyncio.create_task(self._run_swift_stt(wav_path, is_wake_check=True, locale=_sp_locale))
                    whisper_t = asyncio.create_task(self._run_whisper_stt(_whisper_input, language=_sp_lang))
                    name_map = {id(swift_t): "Swift", id(whisper_t): "Whisper"}
                    pending = {swift_t, whisper_t}
                    while pending and not raw_text:
                        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                        for t in done:
                            try:
                                text, meta = t.result()
                                if text:
                                    raw_text = text
                                    stt_meta = meta
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
                # 🇹🇼 台語講者（NAN_SPEAKER_IDS）走雅婷雲端 STT；輸出已正規化成華語，下游零改動。
                # 空/失敗自動降級回下面既有 Swift→Groq 鏈（優雅降級）。
                if self._is_nan_speaker(user_id):
                    print(f"🎙️ [Engine] 台語講者 → 雅婷雲端 STT (Speaker: {speaker_name})...", flush=True)
                    raw_text, stt_meta = await self._run_yating_stt(whisper_audio)
                    if raw_text:
                        used_engine = "Yating"
                        print(f"✅ [STT Output] {speaker_name}: {raw_text} (Engine: Yating)", flush=True)

                # 序列備援：Swift server 優先（最高準確度），失敗才用 Whisper
                if not raw_text:
                    print(f"🎙️ [Engine] 啟動 macOS Native Swift STT (Speaker: {speaker_name}, Locale: {_sp_locale})...", flush=True)
                    raw_text, stt_meta = await self._run_swift_stt(wav_path, is_wake_check=False, locale=_sp_locale)
                    if raw_text:
                        used_engine = "Swift"
                        print(f"✅ [STT Output] {speaker_name}: {raw_text} (Engine: Swift)", flush=True)
                # Swift 失敗：Apple platform 用 Groq Whisper API 備援，Linux 用 Faster-Whisper
                # 設 STT_SWIFT_STRICT=true 可關閉 fallback（避免 Whisper 在雜音上幻覺）
                # 注：_swift_strict 已在函式頂部定義，這裡直接用
                if not raw_text and _is_apple_platform and os.getenv("GROQ_API_KEY") and not _swift_strict:
                    print(f"🎙️ [Engine] 啟動備援 Groq Whisper (Speaker: {speaker_name}, Lang: {_sp_lang})...", flush=True)
                    raw_text, stt_meta = await self._run_groq_whisper_stt(wav_path, language=_sp_lang)
                    if raw_text:
                        used_engine = "Groq"
                        print(f"✅ [STT Output] {speaker_name}: {raw_text} (Engine: Groq)", flush=True)
                elif not raw_text and self.whisper_model and not _is_apple_platform:
                    print(f"🎙️ [Engine] 啟動備援 Faster-Whisper 辨識 (Speaker: {speaker_name})...", flush=True)
                    raw_text, stt_meta = await self._run_whisper_stt(_whisper_input, language=_sp_lang)
                    if raw_text:
                        used_engine = "Whisper"
                        print(f"✅ [STT Output] {speaker_name}: {raw_text} (Engine: Whisper)", flush=True)

            pipeline_timing.mark("stt_done")
            # Update speaker language memory from this utterance for next call
            if raw_text:
                self._update_speaker_lang(speaker_name, raw_text)
            # STT meta（avg/min confidence、prosody）：先 log 紀錄，後續供 J1 信心校準
            print(f"🧪 [STT Meta DEBUG] engine={used_engine} speaker={speaker_name} meta={stt_meta} type={type(stt_meta).__name__}", flush=True)
            if stt_meta:
                logger.info(f"[STT Meta] {used_engine} speaker={speaker_name} {stt_meta}")

        finally:
            _lock.release()

        # 2026-05-20: STT 完成 + stt_lock 釋放後立刻釋放 inflight slot。
        # cleaner LLM（下方 Track B clean_stt_text）在 slot 外跑——Groq 8b 429
        # 慢 cleaner 不再佔住 STT 名額餓死其他人的 wake_check，Cerebras 寬 quota
        # 自然吸收。caller 的 finally idempotent 兜底（STT 早退時補釋放）。
        if release_inflight is not None:
            release_inflight()

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
            clean_res = None  # 預先 init：cleaner block 未進入時下方 should_callback
                              # 路徑 access clean_res.get(...) 不致 UnboundLocalError

            if hasattr(self.bot, 'router') and hasattr(self.bot.router, 'clean_stt_text'):
                # Phase 2: 計算對話脈絡訊號
                _now = time.time()
                _recent_10 = self.conv_buffer.get_last_n_utterances(10)
                # Marvin 最近發話年齡（秒），無 → inf
                _marvin_ages = [(_now - e["timestamp"]) for e in _recent_10 if e["speaker"] == "Marvin"]
                _marvin_age = min(_marvin_ages) if _marvin_ages else float("inf")
                # context_active: Marvin 在 90s 內說過話（對話進行中）
                context_active = _marvin_age <= 90.0
                # marvin_in_echo_window: 0-2s（含），TTS 尾音/麥克回授高風險窗 → 拉高 wake threshold
                marvin_in_echo_window = _marvin_age <= 2.0
                # marvin_just_spoke: 2-15s 後續視窗，使用者最可能此時呼叫 → 降低 threshold（不含 echo 區段）
                marvin_just_spoke = (2.0 < _marvin_age <= 15.0)
                recent_ctx = self.conv_buffer.get_last_n_utterances(5)
                clean_res = await self.bot.router.clean_stt_text(
                    raw_text, context=recent_ctx,
                    speaker=speaker_name, context_active=context_active,
                    marvin_just_spoke=marvin_just_spoke,
                    marvin_in_echo_window=marvin_in_echo_window,
                    apply_gate=True,   # 🚪 只有 wake-check 路徑 gate；無訊號+非對話 → 略過 cleaner
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

                # Phase 2 false-wake proxy: if harvest is empty a few seconds after a
                # Track B wake, that's a likely false wake — feed signal back to WakeSignalFusion
                if is_wake_B and not is_wake_A:
                    _ts = timestamp
                    _spk = speaker_name
                    async def _check_false_wake():
                        # 2026-06-04 修：舊 wait=1.1s / after=1.0 太緊。真實後續命令是「說出於
                        # _ts+~2.5s、STT 再延遲 2-3s 才落地」，整段被窗口錯過 → 合法召喚被誤標
                        # false（實測 showay 3 筆有 2 筆當下其實在 active 對話），還反向餵 fusion
                        # 調高該說話者門檻。放寬到 wait 5s（等 STT 落地）+ after 3.0（涵蓋後續發言）。
                        await asyncio.sleep(5.0)
                        harvest = self.conv_buffer.get_harvest(_ts, before=3.0, after=3.0)
                        is_false = len(harvest.strip()) < 5
                        # 品質指標 capture：每次 Track-B wake 都記一筆（真 wake 當分母才算得出 rate）。
                        # 在延遲 task 內、非熱路徑，同步 append 安全。
                        record_metric("false_responding", speaker=_spk, track="B",
                                      was_false=is_false,
                                      reason="empty_harvest" if is_false else "harvest_ok")
                        if is_false:
                            fusion = getattr(getattr(self, 'bot', None), 'router', None)
                            fusion = getattr(fusion, 'wake_fusion', None) if fusion else None
                            if fusion:
                                fusion.record_outcome(_spk, False)
                                logger.info(f"📊 [FusionFeedback] Empty harvest → false wake recorded for {_spk}")
                    asyncio.create_task(_check_false_wake())
        elif not raw_text:
            print(f"🔇 [Engine] {speaker_name} 辨識完畢，但無文字內容。", flush=True)

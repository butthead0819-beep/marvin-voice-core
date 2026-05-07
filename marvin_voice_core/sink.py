import discord
import time
import asyncio
import discord.opus
from collections import deque
try:
    import davey
except ImportError:
    davey = None

from .audio_utils import calculate_rms
from discord.ext import voice_recv

class RealtimeVADSink(voice_recv.AudioSink):
    """
    基於 voice_recv 的純淨 PCM 切片器 (與 DAVE 解密整合版)
    """
    def __init__(self, on_speech_cut_callback, on_speech_start_callback=None, temperature_callback=None, sink_error_callback=None, suppress_wake_callback=None):
        super().__init__()
        self.on_speech_cut_callback = on_speech_cut_callback
        self.on_speech_start_callback = on_speech_start_callback
        self.temperature_callback = temperature_callback
        self.sink_error_callback = sink_error_callback
        # 當串流/電台播放中，外部可注入此 callback 回傳 True 來抑制喚醒偵測
        self.suppress_wake_callback = suppress_wake_callback
        self.meta_analyzer = None
        self.user_buffers = {}
        self.user_last_spoken_time = {}
        self.user_last_packet_time = {}
        self.RMS_THRESHOLD = 200
        self.RMS_THRESHOLD_STREAM = 500  # 串流播放時提高門檻，過濾擴音回聲
        self.user_near_silence_count = {}
        self.user_first_audio_time = {}
        self.user_wake_check_done = {}   # 🚀 [新增]
        self.decoders = {}
        
        # 🚀 [Adaptive Noise Floor]
        self.user_noise_stats = {}      # user_id -> {'sum_x': 0.0, 'sum_x2': 0.0, 'history': deque(maxlen=75)}
        self.user_noise_floor = {}      # user_id -> current noise floor baseline
        self.user_is_speaking = {}      # user_id -> bool
        
        self.loop = asyncio.get_event_loop()  # set properly via set_loop() after instantiation
        self.packet_count = 0
        self.last_audio_packet_time = time.time() 

    def wants_opus(self) -> bool:
        return True

    def write(self, user: discord.User | discord.Member | None, data: voice_recv.VoiceData):
        if not user:
            return

        self.packet_count += 1
        self.last_audio_packet_time = time.time()

        user_id = user.id
        
        try:
            dave_session = getattr(self.voice_client._connection, 'dave_session', None)
            opus_data = data.opus
            if not opus_data:
                return

            final_opus = opus_data
            if davey and dave_session and dave_session.ready:
                try:
                    final_opus = dave_session.decrypt(user_id, davey.MediaType.audio, opus_data)
                except Exception as e:
                    error_msg = str(e)
                    if "UnencryptedWhenPassthroughDisabled" in error_msg:
                        final_opus = opus_data
                    else:
                        if self.packet_count % 50 == 0:
                            print(f"❌ [Core_Sink DAVE Error] UserID: {user_id}, Error: {e}", flush=True)
                        
                        low_error = error_msg.lower()
                        if "decrypt" in low_error or "crypto" in low_error:
                            now = time.time()
                            if self.sink_error_callback and (now - getattr(self, 'last_dave_error_time', 0) > 5):
                                self.last_dave_error_time = now
                                self.sink_error_callback("decryption_failed")
                        return
            elif self.packet_count % 100 == 0 and dave_session:
                 print(f"📡 [Core_Sink] DAVE Session 尚未就緒，嘗試透傳 (Packet: {self.packet_count})", flush=True)

            if user_id not in self.decoders:
                print(f"🎹 [Core_Sink] 為使用者 {user.name} 建立新的 Opus Decoder", flush=True)
                self.decoders[user_id] = discord.opus.Decoder()
            
            pcm_bytes = self.decoders[user_id].decode(final_opus)

            if user_id not in self.user_buffers:
                self.user_buffers[user_id] = bytearray()
                
            self.user_buffers[user_id].extend(pcm_bytes)
            
            now = time.time()
            rms = calculate_rms(pcm_bytes)

            suppressing = self.suppress_wake_callback() if self.suppress_wake_callback else False
            
            # --- [Adaptive Noise Floor Implementation] ---
            if user_id not in self.user_noise_stats:
                self.user_noise_stats[user_id] = {'sum_x': 0.0, 'sum_x2': 0.0, 'history': deque(maxlen=75)}
                self.user_noise_floor[user_id] = 50.0
                self.user_is_speaking[user_id] = False

            stats = self.user_noise_stats[user_id]
            
            # Deadlock Recovery
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
                variance = max(0.0, (stats['sum_x2'] - (stats['sum_x'] ** 2) / count) / count)
                
                if variance < 1600.0:
                    self.user_noise_floor[user_id] = float(mean_rms)
            
            noise_floor = self.user_noise_floor[user_id]
            
            # 動態閾值
            delta_threshold = 100
            dynamic_threshold = max(self.RMS_THRESHOLD, noise_floor + delta_threshold)
            
            if suppressing:
                dynamic_threshold = max(self.RMS_THRESHOLD_STREAM, dynamic_threshold)
            
            snr_threshold = noise_floor * 1.5
            active_threshold = max(dynamic_threshold, snr_threshold)
            # --- End Adaptive Noise Floor ---

            if rms > active_threshold:
                if not self.user_is_speaking[user_id]:
                    self.user_is_speaking[user_id] = True
                    if self.on_speech_start_callback:
                        self.on_speech_start_callback(user_id)
                
                if self.user_last_spoken_time.get(user_id, 0) == 0:
                    self.user_first_audio_time[user_id] = now
                self.user_last_spoken_time[user_id] = now
                self.user_near_silence_count[user_id] = 0

                # 🚀 [新增] 喚醒詞快速通道（串流播放中停用，避免擴音回聲誤觸發）
                if not suppressing and \
                   (now - self.user_first_audio_time.get(user_id, now)) > 1.8 and \
                   not self.user_wake_check_done.get(user_id, False):
                    self.user_wake_check_done[user_id] = True
                    audio_snapshot = bytes(self.user_buffers[user_id])
                    self.loop.create_task(
                        self.on_speech_cut_callback(user_id, audio_snapshot, self.user_first_audio_time[user_id], is_wake_check=True)
                    )
            else:
                if rms > 50:
                    self.user_near_silence_count[user_id] = self.user_near_silence_count.get(user_id, 0) + 1
                
                # 🚀 [新增] 事件驅動靜音偵測
                last_spoken = self.user_last_spoken_time.get(user_id, 0)
                if last_spoken > 0:
                    threshold = self.temperature_callback() if self.temperature_callback else 1.2
                    if now - last_spoken > threshold:
                        if len(self.user_buffers[user_id]) > 19200:
                            audio_data = bytes(self.user_buffers[user_id])
                            self.user_buffers[user_id] = bytearray()
                            self.user_last_spoken_time[user_id] = 0
                            self.user_wake_check_done.pop(user_id, None)
                            self.user_is_speaking[user_id] = False
                            self.loop.create_task(self.on_speech_cut_callback(user_id, audio_data, last_spoken))
                        else:
                            self.user_buffers[user_id] = bytearray()
                            self.user_last_spoken_time[user_id] = 0
                            self.user_wake_check_done.pop(user_id, None)
                            self.user_is_speaking[user_id] = False
            
            if self.meta_analyzer:
                self.meta_analyzer.add_rms(user_id, rms)
            
            self.user_last_packet_time[user_id] = now

        except Exception as e:
            if self.packet_count % 50 == 0:
                print(f"⚠️ [Core_Sink.write Warning] {e}", flush=True)

    def cleanup(self):
        self.user_buffers.clear()
        self.user_last_spoken_time.clear()
        self.user_first_audio_time.clear()
        self.decoders.clear()
        self.user_noise_stats.clear()
        self.user_noise_floor.clear()
        self.user_is_speaking.clear()
        print("🧹 [Core_Sink] 已清理資源。", flush=True)

    async def _flush_user(self, user_id, timestamp):
        if user_id in self.user_buffers and len(self.user_buffers[user_id]) > 0:
            audio_data = bytes(self.user_buffers[user_id])
            self.user_buffers[user_id] = bytearray()
            if user_id in self.user_last_spoken_time:
                del self.user_last_spoken_time[user_id]
            self.user_wake_check_done.pop(user_id, None)
            self.user_is_speaking[user_id] = False
            
            self.loop.create_task(self.on_speech_cut_callback(user_id, audio_data, timestamp))

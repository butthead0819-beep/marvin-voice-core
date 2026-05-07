import asyncio
import time
import os
from .sink import RealtimeVADSink
from .stt_handler import STTHandler
from .audio_utils import apply_gain, save_wav, calculate_rms

class ConversationBuffer:
    """
    對話滾動緩衝區，用於計算交談溫度。
    """
    def __init__(self, max_minutes=6):
        self.max_seconds = max_minutes * 60
        self.history = [] 
        self.has_new_messages = False 

    def add_entry(self, speaker, text, timestamp=None):
        if timestamp is None:
            timestamp = time.time()
        self.history.append({"timestamp": timestamp, "speaker": speaker, "text": text})
        self.has_new_messages = True 
        self._prune()

    def _prune(self):
        now = time.time()
        self.history = [e for e in self.history if now - e["timestamp"] <= self.max_seconds]

    def get_history(self):
        self._prune()
        return self.history

    def get_conversation_temperature(self, window_seconds=60) -> float:
        """
        動態決定 VAD 截斷閾值
        High (>8): 3.0s | Normal (3~8): 1.5s | Low (<3): 0.8s
        """
        now = time.time()
        recent_utterances = len([e for e in self.history if now - e["timestamp"] <= window_seconds])
        
        if recent_utterances > 8:
            return 3.0 
        elif 3 <= recent_utterances <= 8:
            return 1.5 
        else:
            return 0.8 

class MarvinVoicePipeline:
    """
    核心語音處理流水線。
    """
    def __init__(self, bot, whisper_model=None):
        self.bot = bot
        self.stt_handler = STTHandler(whisper_model=whisper_model)
        self.stt_callback = None
        self.speech_start_callback = None
        self.sink_error_callback = None
        self.stt_lock = asyncio.Semaphore(1)
        self.is_listening = True
        self.sink = None
        
        self.audio_buffers = {} # user_id -> {pcm: bytearray, first_start: float}
        self.MAX_AUDIO_CHUNK_DURATION = 12.0 
        self.conv_buffer = ConversationBuffer(max_minutes=6)
        self._watchdog_task = None
        self.meta_analyzer = None # 由外部注入

    def start(self):
        self.is_listening = True
        if self._watchdog_task and not self._watchdog_task.done():
            return
        self._watchdog_task = self.bot.loop.create_task(self._vad_watchdog())

    def stop(self):
        self.is_listening = False
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()

    def create_sink(self):
        self.sink = RealtimeVADSink(
            on_speech_cut_callback=self.process_audio_slice,
            on_speech_start_callback=self._handle_raw_speech_start,
            sink_error_callback=self.sink_error_callback
        )
        if self.meta_analyzer:
            self.sink.meta_analyzer = self.meta_analyzer
        return self.sink

    async def _vad_watchdog(self):
        print("🛡️ [Core_Pipeline] VAD Watchdog 已啟動。", flush=True)
        while self.is_listening:
            await asyncio.sleep(0.5)
            now = time.time()
            sink = self.sink
            if not sink:
                continue
                
            stt_vad_threshold = self.conv_buffer.get_conversation_temperature()
            
            for user_id in list(sink.user_buffers.keys()):
                last_spoken = sink.user_last_spoken_time.get(user_id, 0)
                
                # 情境 A: 偵測到靜默
                if last_spoken > 0 and (now - last_spoken > stt_vad_threshold):
                    buffer_bytes = len(sink.user_buffers[user_id])
                    if buffer_bytes > 19200:
                        audio_data = bytes(sink.user_buffers[user_id])
                        sink.user_buffers[user_id] = bytearray()
                        sink.user_last_spoken_time[user_id] = 0 
                        asyncio.create_task(self.process_audio_slice(user_id, audio_data, last_spoken))
                    else:
                        sink.user_buffers[user_id] = bytearray()
                        sink.user_last_spoken_time[user_id] = 0
                
                # 情境 B: 說太長了
                first_audio = sink.user_first_audio_time.get(user_id, 0)
                if first_audio > 0 and (now - first_audio > self.MAX_AUDIO_CHUNK_DURATION):
                    buffer_bytes = len(sink.user_buffers[user_id])
                    if buffer_bytes > 19200:
                        audio_data = bytes(sink.user_buffers[user_id])
                        sink.user_buffers[user_id] = bytearray()
                        sink.user_last_spoken_time[user_id] = now # 持續更新說話時間
                        sink.user_first_audio_time[user_id] = now 
                        asyncio.create_task(self.process_audio_slice(user_id, audio_data, first_audio))
                    else:
                        sink.user_buffers[user_id] = bytearray()
                        sink.user_first_audio_time[user_id] = 0

    def _handle_raw_speech_start(self, user_id):
        if self.speech_start_callback:
            speaker_name = f"User_{user_id}"
            for guild in self.bot.guilds:
                member = guild.get_member(user_id)
                if member:
                    speaker_name = member.nick if member.nick else member.display_name
                    break
            self.speech_start_callback(speaker_name, user_id=user_id)

    async def process_audio_slice(self, user_id, raw_pcm, start_time):
        if user_id not in self.audio_buffers:
            self.audio_buffers[user_id] = {"pcm": bytearray(), "first_start": start_time}
        self.audio_buffers[user_id]["pcm"].extend(raw_pcm)
        await self._flush_audio_to_stt(user_id)

    async def _flush_audio_to_stt(self, user_id):
        if user_id not in self.audio_buffers:
            return
            
        data = self.audio_buffers.pop(user_id)
        raw_pcm = bytes(data["pcm"])
        start_time = data["first_start"]
        
        try:
            rms = calculate_rms(raw_pcm)
            duration = len(raw_pcm) / (48000 * 2 * 2)
            
            # 自動增益
            processed_pcm = raw_pcm
            if 100 < rms < 2500:
                processed_pcm = apply_gain(raw_pcm, 1.8)
            
            wav_path = f"tmp_stt_{user_id}_{int(start_time)}.wav"
            abs_wav_path = save_wav(processed_pcm, wav_path)
            try:
                with open(abs_wav_path, 'rb') as f:
                    wav_bytes = f.read()

                speaker_name = f"User_{user_id}"
                for guild in self.bot.guilds:
                    member = guild.get_member(user_id)
                    if member:
                        speaker_name = member.nick if member.nick else member.display_name
                        break

                # stt_lock 只保護 Swift 轉錄（序列化 STT subprocess），
                # handle_stt_result 含 Groq API 等待，不能鎖在裡面
                async with self.stt_lock:
                    game_dict = getattr(self.bot.router, 'game_dict_string', "") if hasattr(self.bot, 'router') else ""
                    raw_text, engine = await self.stt_handler.transcribe_hybrid(
                        abs_wav_path, speaker_name, game_dict_string=game_dict
                    )

                if self.stt_callback and raw_text:
                    prosody_data = None
                    if self.meta_analyzer:
                        prosody_data = self.meta_analyzer.calculate_prosody(user_id, "placeholder", duration)
                        if prosody_data:
                            clean_len = len(raw_text.replace(" ", ""))
                            prosody_data["wps"] = round(clean_len / prosody_data["physical_duration"], 2) if prosody_data["physical_duration"] > 0 else 0

                    await self.stt_callback(speaker_name, raw_text, start_time, wav_bytes, prosody_data=prosody_data)
            finally:
                if os.path.exists(abs_wav_path):
                    os.remove(abs_wav_path)
        except Exception as e:
            print(f"[Core_Pipeline Error] Audio flush failed: {e}")

    async def clear_buffers(self):
        """🚀 [Chief Architect Action] 徹底清空所有待處理的語音"""
        print("🧹 [Core_Pipeline] 正在執行 Phantom Purge，清空所有語音緩衝區...", flush=True)
        # 1. 清空切片緩衝區
        self.audio_buffers = {}
        # 2. 清空 Sink 緩衝區
        if self.sink:
            self.sink.user_buffers = {}
            self.sink.user_last_spoken_time = {}
            self.sink.user_first_audio_time = {}
        print("✅ [Core_Pipeline] 緩衝區已歸零。", flush=True)

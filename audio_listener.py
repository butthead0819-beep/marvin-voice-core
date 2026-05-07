import sys
import time
import json
import asyncio
import threading
import collections
import numpy as np
import pyaudio
from vosk import Model, KaldiRecognizer
from typing import Callable, Optional, Awaitable

class AudioEngine:
    """
    High-performance audio engine for real-time listening, VAD slicing, and wake-word detection.
    Strictly locked to 16000Hz, Mono, 16-bit PCM for optimal STT accuracy.
    """
    RATE = 16000
    CHANNELS = 1
    FORMAT = pyaudio.paInt16
    CHUNK_SIZE = 1024  # ~64ms per chunk (1024/16000)

    def __init__(self, 
                 model_path: str = "model/en",
                 input_device_index: Optional[int] = None,
                 on_wake_word_detected: Optional[Callable[[float], Awaitable[None]]] = None,
                 on_speech_chunk_ready: Optional[Callable[[bytes, float], Awaitable[None]]] = None,
                 rms_threshold: int = 500):
        """
        Initializes the audio engine.
        
        Args:
            model_path: Path to the Vosk English model directory.
            input_device_index: Specific device index to use (None for default).
            on_wake_word_detected: Async callback for wake-word events.
            on_speech_chunk_ready: Async callback for VAD speech segments.
            rms_threshold: Energy threshold for VAD (default 500).
        """
        self.input_device_index = input_device_index
        self.on_wake = on_wake_word_detected
        self.on_speech = on_speech_chunk_ready
        self.rms_threshold = rms_threshold
        
        # 1. Vosk Initialization (Heavy I/O)
        try:
            self.model = Model(model_path)
            self.recognizer = KaldiRecognizer(self.model, self.RATE)
        except Exception as e:
            print(f"[AudioEngine] FATAL: Could not load Vosk model from '{model_path}'.")
            print(f"Error: {e}")
            raise

        self._p = pyaudio.PyAudio()
        self._list_devices()
        
        self.is_running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._worker_thread: Optional[threading.Thread] = None

    def _list_devices(self):
        """Lists all available input devices to Console as requested by architect."""
        print("\n[AudioEngine] --- Available Input Devices ---")
        default_idx = self._p.get_default_input_device_info().get('index')
        
        for i in range(self._p.get_device_count()):
            info = self._p.get_device_info_by_index(i)
            if info['maxInputChannels'] > 0:
                is_default = " (DEFAULT)" if i == default_idx else ""
                print(f"Index {i}: {info['name']} (Channels: {info['maxInputChannels']}){is_default}")
        print("------------------------------------------\n")

    async def start_listening(self) -> None:
        """
        Starts the continuous microphone listening loop.
        Offloads the blocking audio acquisition to a background thread to prevent lag.
        """
        self._loop = asyncio.get_running_loop()
        self.is_running = True
        
        # Start processing in a dedicated daemon thread
        self._worker_thread = threading.Thread(target=self._listening_thread_worker, daemon=True)
        self._worker_thread.start()
        print("[AudioEngine] Listening thread started successfully.")

    def _listening_thread_worker(self):
        """
        Internal worker thread: Handles PyAudio blocking I/O and CPU-bound math/Vosk logic.
        Communicates back to the main thread via call_soon_threadsafe.
        """
        try:
            stream = self._p.open(
                format=self.FORMAT,
                channels=self.CHANNELS,
                rate=self.RATE,
                input=True,
                input_device_index=self.input_device_index,
                frames_per_buffer=self.CHUNK_SIZE
            )
        except Exception as e:
            print(f"[AudioEngine] ERROR: Could not open microphone stream: {e}")
            return

        # VAD Slicing States
        is_speaking = False
        speech_buffer = []
        chunk_start_time = 0.0
        silence_start_time = 0.0
        SILENCE_DURATION_LIMIT = 0.8 # 800ms threshold

        print(f"[AudioEngine] Stream active (Index: {self.input_device_index if self.input_device_index is not None else 'Default'})")

        try:
            while self.is_running:
                data = stream.read(self.CHUNK_SIZE, exception_on_overflow=False)
                timestamp = time.time()
                
                # --- Track 1: Vosk Wake-Word Detection (Fast System) ---
                if self.recognizer.AcceptWaveform(data):
                    res = json.loads(self.recognizer.Result())
                    text = res.get("text", "").lower()
                    if "suki says" in text:
                        self._trigger_wake(timestamp)
                else:
                    # Partial result for faster wake detection
                    partial = json.loads(self.recognizer.PartialResult())
                    p_text = partial.get("partial", "").lower()
                    if "suki says" in p_text:
                        self._trigger_wake(timestamp)
                        # Reset recognizer to prevent repeated triggers from the same phrase
                        self.recognizer.Reset()

                # --- Track 2: RMS-based VAD (Lightweight Architecture) ---
                audio_array = np.frombuffer(data, dtype=np.int16)
                rms = np.sqrt(np.mean(audio_array.astype(np.float32)**2))
                
                if rms > self.rms_threshold:
                    if not is_speaking:
                        # Character starts speaking
                        is_speaking = True
                        speech_buffer = [data]
                        chunk_start_time = timestamp
                        # print(f"[VAD] Speech Detected (RMS: {rms:.0f})")
                    else:
                        speech_buffer.append(data)
                    silence_start_time = 0.0
                else:
                    if is_speaking:
                        # Append the 'silent' chunk to ensure no truncation
                        speech_buffer.append(data)
                        if silence_start_time == 0.0:
                            silence_start_time = timestamp
                        
                        # Check if silence duration exceeded 0.8s
                        if (timestamp - silence_start_time) >= SILENCE_DURATION_LIMIT:
                            audio_bytes = b"".join(speech_buffer)
                            # Notify STT system
                            self._trigger_speech(audio_bytes, chunk_start_time)
                            is_speaking = False
                            speech_buffer = []

        except Exception as thread_e:
            print(f"[AudioEngine] Internal Loop Error: {thread_e}")
        finally:
            stream.stop_stream()
            stream.close()

    def _trigger_wake(self, timestamp: float):
        """Bridges the wake trigger back to the asyncio loop safely."""
        if self.on_wake and self._loop:
            # Note: We use call_soon_threadsafe + create_task to bridge to async
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self.on_wake(timestamp))
            )

    def _trigger_speech(self, audio_bytes: bytes, start_time: float):
        """Bridges a new speech chunk back to the asyncio loop safely."""
        if self.on_speech and self._loop:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self.on_speech(audio_bytes, start_time))
            )

    def stop(self):
        """Stops the audio engine and releases hardware resources."""
        self.is_running = False
        # Small delay to allow thread to close
        time.sleep(0.1)
        self._p.terminate()

if __name__ == "__main__":
    # --- Self-Testing Strategy ---
    
    async def mock_wake_callback(ts):
        # Format for clear visual distinction
        print(f"\n🔔 [Vosk] 喚醒觸發！偵測時間: {time.strftime('%H:%M:%S', time.localtime(ts))}")

    async def mock_speech_callback(data, ts):
        duration = len(data) / (AudioEngine.RATE * 2) # 2 bytes per sample
        print(f"✅ [VAD] 切片完成 | 開口時間: {time.strftime('%H:%M:%S', time.localtime(ts))} | 長度: {duration:.2f}s")

    async def test_session():
        print("=== Task 2.2: AudioEngine Stress Test ===")
        print("[Note] Please ensure 'model/en' contains a valid Vosk English model.")
        
        try:
            # RMS Threshold 500 is good for typical background noise; adjust if needed.
            engine = AudioEngine(
                model_path="model/en", 
                on_wake_word_detected=mock_wake_callback,
                on_speech_chunk_ready=mock_speech_callback,
                rms_threshold=500
            )
            
            await engine.start_listening()
            print("[Test] Listening... Say 'Suki says' or speak into the mic.")
            print("[Test] Press Ctrl+C to terminate.")
            
            # Keep main loop alive
            while True:
                await asyncio.sleep(1)
                
        except KeyboardInterrupt:
            print("\n[Test] Terminating test...")
            engine.stop()
        except Exception as e:
            print(f"\n[Test Error] {e}")

    # Run the test
    asyncio.run(test_session())

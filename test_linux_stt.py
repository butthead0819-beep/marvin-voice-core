"""
Linux STT pipeline smoke test.
Generates a Chinese TTS audio clip via edge-tts, converts to WAV,
passes it through STTHandler (Whisper path), and prints the result.

Run inside Docker:
  docker run --rm marvin-linux-test python test_linux_stt.py
"""
import asyncio
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))


async def main():
    # 1. Generate test speech with edge-tts
    print("🔊 Generating test audio via edge-tts...")
    try:
        import edge_tts
    except ImportError:
        print("❌ edge-tts not installed")
        sys.exit(1)

    test_text = "馬文你好，這是 Linux 語音辨識測試。"
    mp3_fd, mp3_path = tempfile.mkstemp(suffix=".mp3")
    os.close(mp3_fd)
    wav_fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(wav_fd)

    try:
        communicate = edge_tts.Communicate(test_text, voice="zh-TW-YunJheNeural")
        await communicate.save(mp3_path)
        print(f"✅ TTS audio saved to {mp3_path}")

        # 2. Convert MP3 → 16kHz mono WAV (same format as Discord PCM pipeline)
        print("🔄 Converting to 16kHz WAV via ffmpeg...")
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", mp3_path, "-ar", "16000", "-ac", "1", wav_path],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"❌ ffmpeg failed:\n{result.stderr}")
            sys.exit(1)
        print(f"✅ WAV ready: {wav_path}")

        # 3. Load Whisper and run STTHandler
        print("🧠 Loading Faster-Whisper (tiny)...")
        from faster_whisper import WhisperModel
        model = WhisperModel("tiny", device="cpu", compute_type="int8")
        print("✅ Whisper model loaded")

        from marvin_voice_core.stt_handler import STTHandler
        handler = STTHandler(whisper_model=model)

        print("🎙️  Running STTHandler (Swift will fail → Whisper fallback expected)...")
        text, engine = await handler.transcribe(wav_path, speaker="TestUser", context="")

        print()
        print("=" * 50)
        print(f"Engine used : {engine}")
        print(f"Input text  : {test_text}")
        print(f"Transcribed : {text}")
        print("=" * 50)

        if engine == "Whisper" and text:
            print("✅ PASS — Linux Whisper STT pipeline works")
        elif engine == "Whisper" and not text:
            print("⚠️  PARTIAL — Whisper ran but returned empty text")
        else:
            print(f"❌ FAIL — unexpected engine: {engine}")
            sys.exit(1)

    finally:
        for p in (mp3_path, wav_path):
            try:
                os.unlink(p)
            except OSError:
                pass


if __name__ == "__main__":
    asyncio.run(main())

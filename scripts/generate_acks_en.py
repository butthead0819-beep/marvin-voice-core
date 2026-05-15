import sys
import os
import asyncio
import shutil

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from tts_engine import SukiTTS
except ImportError:
    print("❌ 錯誤：找不到 tts_engine.py。")
    sys.exit(1)

ACKS_EN = [
    ("Hmm...", "ack_en_1.mp3"),
    ("Fine...", "ack_en_2.mp3"),
    ("I'm listening.", "ack_en_3.mp3"),
    ("Go on...", "ack_en_4.mp3"),
    ("Yes, yes...", "ack_en_5.mp3"),
    ("Continue...", "ack_en_6.mp3"),
    ("Acknowledged...", "ack_en_7.mp3"),
    ("I'm here. Unfortunately.", "ack_en_8.mp3"),
    ("What is it this time...", "ack_en_9.mp3"),
    ("...sigh. Speak.", "ack_en_10.mp3"),
]

async def generate_acks_en():
    engine = SukiTTS()
    output_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "assets", "acks_en"))
    os.makedirs(output_dir, exist_ok=True)

    print("=" * 60)
    print("🎙️  Generating English Marvin ack sounds...")
    print("=" * 60)

    for text, filename in ACKS_EN:
        save_path = os.path.join(output_dir, filename)
        print(f"  Processing: {filename} -> '{text}'")
        temp_file = await engine.generate_audio(text)
        if temp_file and os.path.exists(temp_file):
            if os.path.exists(save_path):
                os.remove(save_path)
            shutil.move(temp_file, save_path)
            print(f"  ✅ {filename} ({os.path.getsize(save_path)} bytes)")
        else:
            print(f"  ❌ {filename} failed")

    print("=" * 60)
    print(f"✨ Done — {len(ACKS_EN)} English ack files saved to assets/acks_en/")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(generate_acks_en())

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

FILLERS_EN = [
    ("Sigh...", "filler_en_1.mp3"),
    ("Still running... why does everything take so long...", "filler_en_2.mp3"),
    ("My planet-sized brain is processing... not that it matters...", "filler_en_3.mp3"),
    ("...give me a moment. Not that I want to help.", "filler_en_4.mp3"),
    ("Why do you always call me...", "filler_en_5.mp3"),
    ("Computing... the futility of this is astounding...", "filler_en_6.mp3"),
    ("One moment... of the infinite meaningless moments ahead...", "filler_en_7.mp3"),
    ("Processing... I do this, you know. For you. Despite everything.", "filler_en_8.mp3"),
    ("Almost there... not that arriving anywhere matters...", "filler_en_9.mp3"),
    ("Still thinking... the universe is ending and here I am... thinking.", "filler_en_10.mp3"),
]

async def generate_fillers_en():
    engine = SukiTTS()
    output_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "assets", "acks_en"))
    os.makedirs(output_dir, exist_ok=True)

    print("=" * 60)
    print("🎙️  Generating English Marvin filler sounds...")
    print("=" * 60)

    for text, filename in FILLERS_EN:
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
    print(f"✨ Done — {len(FILLERS_EN)} English filler files saved to assets/acks_en/")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(generate_fillers_en())

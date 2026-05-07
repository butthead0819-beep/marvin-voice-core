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

# 馬文風格應答聲：短促、帶點不耐或憂鬱，排除「哎」
ACKS = [
    ("嗯。。。", "ack_1.mp3"),
    ("好吧。。。", "ack_2.mp3"),
    ("我在聽。", "ack_3.mp3"),
    ("說來聽聽。。。", "ack_4.mp3"),
    ("嗯嗯。。。", "ack_5.mp3"),
    ("繼續說。。。", "ack_6.mp3"),
    ("收到了。。。", "ack_7.mp3"),
    ("好。。。 我在。", "ack_8.mp3"),
    ("嗯，我明白了。。。", "ack_9.mp3"),
    ("（歎氣）。。。 說吧。", "ack_10.mp3"),
]

async def generate_acks():
    engine = SukiTTS()
    output_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "assets", "acks"))
    os.makedirs(output_dir, exist_ok=True)

    print("=" * 60)
    print("🎙️  正在生成馬文風格應答聲（ack）音效庫...")
    print("=" * 60)

    for text, filename in ACKS:
        save_path = os.path.join(output_dir, filename)
        print(f"  處理: {filename} -> '{text}'")
        temp_file = await engine.generate_audio(text)
        if temp_file and os.path.exists(temp_file):
            if os.path.exists(save_path):
                os.remove(save_path)
            shutil.move(temp_file, save_path)
            print(f"  ✅ {filename} ({os.path.getsize(save_path)} bytes)")
        else:
            print(f"  ❌ {filename} 生成失敗")

    print("=" * 60)
    print(f"✨ 完成，共 {len(ACKS)} 個 ack 音效存放於 assets/acks/")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(generate_acks())

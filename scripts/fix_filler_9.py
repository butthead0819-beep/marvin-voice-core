import sys
import os
import asyncio
import shutil

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from tts_engine import SukiTTS

async def run():
    engine = SukiTTS()
    text = "還沒好嗎？"
    save_path = "assets/fillers/filler_9.mp3"
    print(f"🎙️  正在最後補發: filler_9.mp3 -> '{text}'")
    temp_file = await engine.generate_audio(text, emotion="sigh")
    if temp_file and os.path.exists(temp_file):
        shutil.move(temp_file, save_path)
        print(f"✅ 成功最後補發：filler_9.mp3")
    else:
        print("❌ 依舊失敗")

if __name__ == "__main__":
    asyncio.run(run())

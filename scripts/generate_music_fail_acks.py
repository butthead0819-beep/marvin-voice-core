"""生成「無法播放」失敗 ack 音檔。

存放於 assets/acks/music_fail/，由 voice_controller 在點歌失敗時播放：
- yt-dlp 搜不到（_handle_voice_music_command）
- FindSong LLM 識別失敗（_find_song）
- _safe_music_command 意外 exception
"""
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


FAIL_ACKS = [
    ("無法播放", "music_fail.mp3"),
]


async def generate():
    engine = SukiTTS()
    output_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "assets", "acks", "music_fail"))
    os.makedirs(output_dir, exist_ok=True)

    print("=" * 60)
    print("🚫  生成失敗 ack 音效（無法播放）...")
    print("=" * 60)

    for text, filename in FAIL_ACKS:
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
    print(f"✨ 完成，{len(FAIL_ACKS)} 個失敗 ack 音效於 assets/acks/music_fail/")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(generate())

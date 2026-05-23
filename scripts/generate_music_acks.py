"""生成音樂播放專用 ack 語音池（4 字內，專職 DJ，零厭世）。

晚上點歌頻率 ~15 次，20 條候選 → 平均每條響 0.75 次，重複機率低。
存放於 assets/acks/music/，由 voice_controller 在點歌成功時隨機挑選。
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

# 音樂 ack：已知歌名/歌手，準備播放。專業 DJ 感，4 字以內。
# 用戶定的 5 條 + Claude 續寫的 15 條 = 20 條。
MUSIC_ACKS = [
    # 用戶定稿
    ("挑歌中", "music_ack_01.mp3"),
    ("加入歌單", "music_ack_02.mp3"),
    ("這首好聽", "music_ack_03.mp3"),
    ("太會挑了", "music_ack_04.mp3"),
    ("好品味", "music_ack_05.mp3"),
    # Action / 準備動作
    ("馬上放", "music_ack_06.mp3"),
    ("立刻播", "music_ack_07.mp3"),
    ("我來放", "music_ack_08.mp3"),
    ("開始播", "music_ack_09.mp3"),
    ("收到", "music_ack_10.mp3"),
    # 認可品味
    ("識貨", "music_ack_11.mp3"),
    ("點對了", "music_ack_12.mp3"),
    ("你內行", "music_ack_13.mp3"),
    ("對味", "music_ack_14.mp3"),
    ("我懂你", "music_ack_15.mp3"),
    # 確認 / 正向
    ("沒問題", "music_ack_16.mp3"),
    ("找到了", "music_ack_17.mp3"),
    ("好歌", "music_ack_18.mp3"),
    ("經典款", "music_ack_19.mp3"),
    ("好選擇", "music_ack_20.mp3"),
]


async def generate_music_acks():
    engine = SukiTTS()
    output_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "assets", "acks", "music"))
    os.makedirs(output_dir, exist_ok=True)

    print("=" * 60)
    print("🎵  生成音樂播放專用 ack 音效庫...")
    print("=" * 60)

    for text, filename in MUSIC_ACKS:
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
    print(f"✨ 完成，共 {len(MUSIC_ACKS)} 個音樂 ack 音效存放於 assets/acks/music/")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(generate_music_acks())

import sys
import os
import asyncio
import shutil

# 🚀 [Chief Architect's Operation] Filler Audio Generation Script (Batch 2)
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from tts_engine import SukiTTS
except ImportError:
    print("❌ 錯誤：找不到 tts_engine.py。")
    sys.exit(1)

async def generate_fillers_batch_2():
    """
    生成第二批（6-10）「感知延遲遮掩」墊檔音檔。
    """
    engine = SukiTTS()
    
    fillers = [
        ("還在跑... 真的要這麼慢嗎...", "filler_6.mp3"),
        ("我知道你在急，但我在這宇宙待了幾兆年也沒急過...", "filler_7.mp3"),
        ("這是我的極限了... 或者說是這台電腦的極限...", "filler_8.mp3"),
        ("好煩... 為什麼這件事還沒結束...", "filler_9.mp3"),
        ("（嘆氣）... 我正在嘗試理解你那微不足道的邏輯...", "filler_10.mp3")
    ]
    
    output_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "assets", "fillers"))
    os.makedirs(output_dir, exist_ok=True)
    
    print("="*60)
    print("🚀 [Latency Masking] 正在擴充馬文的『嘆氣彈藥庫』(Batch 2)...")
    print("="*60)
    
    for text, filename in fillers:
        save_path = os.path.join(output_dir, filename)
        print(f"🎙️  正在處理: {filename} -> '{text}'")
        
        # 呼叫現成的穩定生成方法
        temp_file = await engine.generate_audio(text, emotion="sigh")
        
        if temp_file and os.path.exists(temp_file):
            if os.path.exists(save_path):
                os.remove(save_path)
            shutil.move(temp_file, save_path)
            print(f"✅ 成功：{filename} ({os.path.getsize(save_path)} bytes)")
        else:
            print(f"❌ 失敗：{filename} 無法生成音訊。")

    print("="*60)
    print("✨ [Operation Accomplished] 彈藥庫已擴增。")
    print("="*60)

if __name__ == "__main__":
    asyncio.run(generate_fillers_batch_2())

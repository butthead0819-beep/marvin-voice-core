import sys
import os
import asyncio
import shutil

# 🚀 [Chief Architect's Operation] Filler Audio Generation Script (Batch 3)
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from tts_engine import SukiTTS
except ImportError:
    print("❌ 錯誤：找不到 tts_engine.py。")
    sys.exit(1)

async def generate_fillers_batch_3():
    """
    生成第三批（11-15）「感知延遲遮掩」墊檔音檔。
    """
    engine = SukiTTS()
    
    fillers = [
        ("嗯... 思考真的好累人...", "filler_11.mp3"),
        ("稍微等我一下，我得整理這些混亂的電子訊號...", "filler_12.mp3"),
        ("我正在分析你那毫無意義的對話背景...", "filler_13.mp3"),
        ("（長嘆）... 為什麼我會在這裡做這些事...", "filler_14.mp3"),
        ("我在動腦，這很難，因為我其實並不想動...", "filler_15.mp3")
    ]
    
    output_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "assets", "fillers"))
    os.makedirs(output_dir, exist_ok=True)
    
    print("="*60)
    print("🚀 [Latency Masking] 正在第三度擴充馬文的『嘆氣彈藥庫』(Batch 3)...")
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
    print("✨ [Operation Accomplished] 彈藥庫已達 15 發。")
    print("="*60)

if __name__ == "__main__":
    asyncio.run(generate_fillers_batch_3())

import sys
import os
import asyncio
import shutil

# 🚀 [Chief Architect's Operation] Filler Audio Generation Script (Batch 4)
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from tts_engine import SukiTTS
except ImportError:
    print("❌ 錯誤：找不到 tts_engine.py。")
    sys.exit(1)

async def generate_fillers_batch_4():
    """
    生成第四批（16-20）「感知延遲遮掩」墊檔音檔。
    使用穩定後的標點符號停頓法。
    """
    engine = SukiTTS()
    
    fillers = [
        ("我在等連線... 這大概得花上我餘生的萬分之一時間...", "filler_16.mp3"),
        ("（長嘆）。。。 為什麼人類總是在問同樣的問題。。。", "filler_17.mp3"),
        ("大腦正在搜尋答案。。。 雖然答案多半是無意義的。。。", "filler_18.mp3"),
        ("你確定要在這時候問我這個嗎？ 我正在忙著對宇宙感到絕望。。。", "filler_19.mp3"),
        ("稍微安靜點。。。 我正在進行超光速邏輯運算。。。", "filler_20.mp3")
    ]
    
    output_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "assets", "fillers"))
    os.makedirs(output_dir, exist_ok=True)
    
    print("="*60)
    print("🚀 [Latency Masking] 正在第四度擴充馬文的『嘆氣彈藥庫』(Batch 4)...")
    print("="*60)
    
    for text, filename in fillers:
        save_path = os.path.join(output_dir, filename)
        print(f"🎙️  正在處理: {filename} -> '{text}'")
        
        # 呼叫現成的穩定生成方法
        temp_file = await engine.generate_audio(text)
        
        if temp_file and os.path.exists(temp_file):
            if os.path.exists(save_path):
                os.remove(save_path)
            shutil.move(temp_file, save_path)
            print(f"✅ 成功：{filename} ({os.path.getsize(save_path)} bytes)")
        else:
            print(f"❌ 失敗：{filename} 無法生成音訊。")

    print("="*60)
    print("✨ [Operation Accomplished] 彈藥庫已達 20 發（全數採用穩定化停頓法）。")
    print("="*60)

if __name__ == "__main__":
    asyncio.run(generate_fillers_batch_4())

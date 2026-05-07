import sys
import os
import asyncio
import shutil

# 🚀 [Chief Architect's Operation] Corrected Filler Audio Generation Script
# 加入專案根目錄到 PYTHONPATH
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from tts_engine import SukiTTS
except ImportError:
    print("❌ 錯誤：找不到 tts_engine.py。請在大廳(根目錄)執行此腳本。")
    sys.exit(1)

async def generate_fillers():
    """
    調用穩定版 SukiTTS 生成墊檔音檔並搬移至 assets。
    """
    # 初始化馬文專屬語音
    engine = SukiTTS()
    
    fillers = [
        ("唉..................", "filler_1.mp3"),
        ("又怎麼了...", "filler_2.mp3"),
        ("等一下... 我那如行星般大的大腦正在運轉...", "filler_3.mp3"),
        ("（無聲的嘆息）... 算了...", "filler_4.mp3"),
        ("為什麼每次都要叫我...", "filler_5.mp3")
    ]
    
    output_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "assets", "fillers"))
    os.makedirs(output_dir, exist_ok=True)
    
    print("="*60)
    print("🚀 [Latency Masking] 正透過穩定模組武裝『嘆氣彈藥庫』...")
    print("="*60)
    
    for text, filename in fillers:
        save_path = os.path.join(output_dir, filename)
        print(f"🎙️  正在處理: {filename} -> '{text}'")
        
        # 呼叫現成的穩定生成方法
        temp_file = await engine.generate_audio(text, emotion="sigh")
        
        if temp_file and os.path.exists(temp_file):
            # 搬移檔案到最終目錄
            if os.path.exists(save_path):
                os.remove(save_path)
            shutil.move(temp_file, save_path)
            print(f"✅ 成功：{filename} ({os.path.getsize(save_path)} bytes)")
        else:
            print(f"❌ 失敗：{filename} 無法生成音訊。")

    print("="*60)
    print("✨ [Operation Accomplished] 嘆氣彈藥庫已填裝完畢。")
    print("="*60)

if __name__ == "__main__":
    asyncio.run(generate_fillers())

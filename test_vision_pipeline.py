"""
直接測試視覺呼叫流程：
1. 截取 monitor 2 三張截圖（模擬 1 FPS 的 3 秒緩衝）
2. 呼叫 analyze_tactical_situation
3. 印出 LLM 回覆
"""
import asyncio
import os
import time
import mss
import cv2
import numpy as np
from dotenv import load_dotenv

load_dotenv()

def capture_frame(sct, monitor_index=2) -> bytes:
    monitor = sct.monitors[monitor_index]
    sct_img = sct.grab(monitor)
    frame = np.array(sct_img)
    frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
    h, w = frame.shape[:2]
    scale = min(1280 / w, 720 / h)
    if scale < 1.0:
        frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    ok, enc = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return enc.tobytes() if ok else None


async def main():
    from gemini_router import GeminiRouter

    print("=== Vision Pipeline Test ===")
    router = GeminiRouter(api_key=os.getenv("GOOGLE_API_KEY"))
    router.current_game = "Overwatch 2"  # 注入遊戲名測試

    # 截 3 張（模擬 3 秒緩衝）
    monitor_index = int(os.getenv("CAPTURE_MONITOR", "2"))
    frames = []
    print(f"[Test] 截取 monitor[{monitor_index}] 共 3 幀...")
    with mss.mss() as sct:
        for i in range(3):
            fb = capture_frame(sct, monitor_index)
            if fb:
                frames.append(fb)
                print(f"  幀 {i+1}: {len(fb):,} bytes")
            await asyncio.sleep(0)  # 讓 event loop 透氣

    if not frames:
        print("❌ 截圖失敗，沒有任何幀")
        return

    # 測試 query
    test_query = "馬文幫我看這個畫面是什麼遊戲"
    print(f"\n[Test] Query: '{test_query}'")
    print(f"[Test] 送出 {len(frames)} 幀給 Gemini Vision...")

    start = time.perf_counter()
    response = await router.analyze_tactical_situation(
        speaker="測試者",
        query_text=test_query,
        frame_bytes=frames,
        extra_context="",
    )
    elapsed = time.perf_counter() - start

    print(f"\n[Result] ({elapsed:.2f}s)")
    print(f"  馬文說: {response}")
    print("\n=== Test Complete ===")


if __name__ == "__main__":
    asyncio.run(main())

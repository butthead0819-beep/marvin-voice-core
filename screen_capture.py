import asyncio
import time
import os
import mss
import cv2
import numpy as np
from collections import deque
from typing import Any, Tuple, Optional, List

class VisualBuffer:
    """
    高效影像暫存緩衝區：
    使用 deque 實作一個滑動窗口 (Sliding Window)，
    保留最近 N 秒的截圖，以便在觸發干預時提供戰術上下文。
    """
    def __init__(self, max_seconds: int = 30, fps: int = 1):
        self.max_size = max_seconds * fps
        self.buffer = deque(maxlen=self.max_size)

    async def add_frame(self, timestamp: float, jpeg_bytes: bytes):
        """將新截圖加入緩衝區 (Thread-safe by nature of deque)"""
        self.buffer.append((timestamp, jpeg_bytes))

    async def get_frames_around(self, target_time: float, before: float = 2.0, after: float = 1.0) -> List[Tuple[float, bytes]]:
        """
        獲取目標時間點前後的影像脈絡。
        """
        start_time = target_time - before
        end_time = target_time + after
        
        # 過濾出符合時間範圍的影格
        return [f for f in self.buffer if start_time <= f[0] <= end_time]

class ScreenCaptureEngine:
    """
    Background engine for high-performance screen capturing and processing.
    Captures primary monitor at 1 FPS, resizes to 720p, and compresses to JPEG.
    Uses asyncio.to_thread to avoid blocking the event loop with CPU-bound image processing.
    """
    def __init__(self, visual_buffer: Any):
        """
        Initializes the engine with a visual buffer instance.
        
        Args:
            visual_buffer: An instance of VisualBuffer (or compatible object) 
                           with an async add_frame(timestamp, jpeg_bytes) method.
        """
        self.visual_buffer = visual_buffer
        self.is_running = False
        self._sct = None # 👁️ [Lifecycle Fix] 初始化為 None，由 start_capture_loop 管理
        self.frame_count = 0 # 🚀 [Debounced Logging] 用於計算擷取次數

    async def start_capture_loop(self) -> None:
        """
        Starts the continuous capture loop at 1 FPS.
        Handles precise timing and offloads heavy processing to workers.
        """
        if self.is_running:
            print("[ScreenCapture] Engine is already running. Skipping start.")
            return

        self.is_running = True
        print("[ScreenCapture] Engine started. Target Frequency: 1 FPS.")
        
        # 🛡️ [Lifecycle Fix] 在迴圈啟動時建立 mss 實例
        self._sct = mss.mss()
        
        try:
            while self.is_running:
                start_loop_time = time.perf_counter()
                
                try:
                    # 1. Offload heavy I/O (screenshot) and CPU-bound (OpenCV resize/encode) to a separate thread.
                    # This ensures the main asyncio event loop remains responsive.
                    timestamp, jpeg_bytes = await asyncio.to_thread(self._capture_and_process)
                    
                    if jpeg_bytes:
                        # 2. Add processed frame to Visual Buffer (Phase 1)
                        await self.visual_buffer.add_frame(timestamp, jpeg_bytes)
                        
                        # 🚀 [Debounced Logging] 每 10 幀印出一行日誌
                        self.frame_count += 1
                        if self.frame_count % 10 == 0:
                            print(f"[ScreenCapture] Frame sequence healthy | Recent: {timestamp:.2f} | Buffer Size: {len(self.visual_buffer.buffer)}")
                    else:
                        print(f"⚠️ [ScreenCapture] Capture failed at {time.time():.2f}: No JPEG data returned.")
                
                except Exception as e:
                    print(f"❌ [ScreenCapture] Runtime ERROR: {e}")
                    import traceback
                    traceback.print_exc()

                # 3. Precise FPS Control: Calculate remaining time to sleep to maintain 1 FPS
                elapsed = time.perf_counter() - start_loop_time
                sleep_time = max(0, 1.0 - elapsed)
                await asyncio.sleep(sleep_time)
        finally:
            # 🛡️ [Lifecycle Fix] 確保結束時關閉 mss，釋放 macOS 錄製資源
            if self._sct:
                print("[ScreenCapture] Closing mss resource.")
                self._sct.close()
                self._sct = None
            self.is_running = False
            print("[ScreenCapture] Engine stopped.")

    def stop(self) -> None:
        """Signals the capture loop to stop."""
        self.is_running = False

    def _capture_and_process(self) -> Tuple[float, Optional[bytes]]:
        """
        Synchronous processing pipeline executed in asyncio.to_thread().
        Includes screen grabbing, color conversion, resizing, and JPEG encoding.
        """
        # Capture raw image from primary monitor
        # Using [1] for the primary monitor
        if not self._sct:
            return time.time(), None
        monitor_index = int(os.getenv("CAPTURE_MONITOR", "1"))
        monitor = self._sct.monitors[monitor_index]
        sct_img = self._sct.grab(monitor)
        
        # Convert to NumPy array (MSS outputs BGRA)
        frame = np.array(sct_img)
        
        # Convert BGRA to BGR for OpenCV compatibility
        frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
        
        # Get dimensions
        h, w = frame.shape[:2]
        target_w, target_h = 1280, 720
        
        # Resize logic: Downscale to 720p if original is larger.
        # If original is smaller, keep original dimensions to avoid pixelation.
        scale = min(target_w / w, target_h / h)
        if scale < 1.0:
            new_w = int(w * scale)
            new_h = int(h * scale)
            frame = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)
        
        # JPEG Compression (Quality 80) into memory
        success, encoded_image = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        
        if not success:
            return time.time(), None
            
        # 🛠️ [Chief Architect Patch] 檢查截圖是否為無效全黑 (macOS 權限問題常見現象)
        # 如果所有像素的平均值極低，代表可能是全黑畫面或是權限被擋住
        if np.mean(frame) < 0.1:
            # 🚀 [Debounced Logging] 每 60 幀 (約 1 分鐘) 才提醒一次，減少日誌噪音
            if self.frame_count % 60 == 0:
                print("[WARNING] ⚠️ 畫面擷取異常：偵測到全黑畫面。請檢查 macOS 螢幕錄製權限。")
            return time.time(), None
            
        return time.time(), encoded_image.tobytes()

if __name__ == "__main__":
    # --- Self-Testing Strategy ---
    
    class DummyVisualBuffer:
        """Minimal mock buffer to verify engine integration."""
        async def add_frame(self, timestamp: float, jpeg_bytes: bytes) -> None:
            # Simulated storage entry
            print(f"  └─ [Buffer Log] 已寫入: {timestamp:.2f}, 尺寸: {len(jpeg_bytes)} Bytes")

    async def main():
        print("=== Task 2.1: ScreenCaptureEngine Test ===")
        mock_buffer = DummyVisualBuffer()
        engine = ScreenCaptureEngine(mock_buffer)
        
        # Run the capture engine in a background task
        loop_task = asyncio.create_task(engine.start_capture_loop())
        
        # Let it run for 5 seconds
        print("[Test] Running for 5 seconds...")
        await asyncio.sleep(5.5) # Slightly longer to ensure we see the log entries
        
        # Graceful shutdown
        print("[Test] Closing engine...")
        engine.stop()
        
        # Wait for loop to terminate and cleanup
        await loop_task
        print("=== Test Completed Successfully ===")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Test] Interrupted by user.")
    except Exception as fatal_e:
        print(f"\n[Test Fatal Error] {fatal_e}")

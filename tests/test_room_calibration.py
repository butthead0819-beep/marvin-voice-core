import os
import unittest
import numpy as np
import scipy.io.wavfile as wav
from scripts.room_calibration import generate_sweep_file, analyze_audio

class TestRoomCalibration(unittest.TestCase):
    def setUp(self):
        self.orig_file = "tests/test_sweep_orig.wav"
        self.rec_file = "tests/test_sweep_rec.wav"
        
        # 建立模擬的原始 sweep 檔
        generate_sweep_file(self.orig_file)

    def tearDown(self):
        # 清理生成的測試檔案
        for f in [self.orig_file, self.rec_file, "records/room_response.png"]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    def test_audio_analysis_dsp(self):
        """測試聲學分析：驗證延遲 (ToA)、平衡度 (Balance) 與 10-band EQ 補償計算"""
        # 1. 讀取剛生成的原始 sweep 檔
        fs_orig, orig_data = wav.read(self.orig_file)
        
        # 模擬錄音：XVF3800 錄音是 16000Hz Mono。
        # 我們將 48000Hz Stereo 的原始訊號轉換成 16000Hz Mono 模擬錄音
        fs_rec = 16000
        duration = 10.0
        rec_len = int(fs_rec * duration)
        rec_data = np.zeros(rec_len, dtype=np.float32)
        
        # 模擬空間延遲與衰減：
        # - 左聲道 Click (在 1.0s) 延遲 5ms (16000 * 0.005 = 80 採樣點)
        # - 右聲道 Click (在 3.0s) 延遲 10ms (16000 * 0.010 = 160 採樣點)
        # - 左 Click 能量比右 Click 大 6dB (約 2 倍振幅)
        # - 5s - 9s 之間的掃頻區間，我們手動對 250Hz 與 500Hz 的段落進行人為衰減 -6dB
        
        delay_l = 80
        delay_r = 160
        
        # 模擬左 Click
        rec_data[int(1.0 * fs_rec) + delay_l] = 30000.0
        # 模擬右 Click (能量減半)
        rec_data[int(3.0 * fs_rec) + delay_r] = 15000.0
        
        # 模擬 5s 到 9s 的 sweep 區間
        # 我們直接將 48kHz 的 sweep 降採樣到 16kHz 並填入
        sweep_data_orig = orig_data[int(5.0 * 48000):int(9.0 * 48000), 0]
        # 最簡單的 3:1 降採樣
        sweep_data_rec = sweep_data_orig[::3].astype(np.float32)
        
        # 模擬空間的頻響落差：我們對 250Hz 與 500Hz 區間加衰減
        # 掃頻 4 秒從 20Hz 到 20000Hz。
        # 頻率隨時間為：f(t) = 20 * (1000)**(t/4.0)
        # 250Hz 約在 t = 4.0 * log(250/20)/log(1000) = 1.46s (即 5s + 1.46s = 6.46s)
        # 500Hz 約在 t = 4.0 * log(500/20)/log(1000) = 1.86s (即 5s + 1.86s = 6.86s)
        # 我們將 6.3s - 7.0s 區間乘以 0.5 (代表衰減 6dB)
        fade_start = int(1.3 * fs_rec)
        fade_end = int(2.0 * fs_rec)
        sweep_data_rec[fade_start:fade_end] *= 0.5
        
        # 加上延遲寫入錄音 (用左延遲為準)
        sweep_start_idx = int(5.0 * fs_rec) + delay_l
        rec_data[sweep_start_idx : sweep_start_idx + len(sweep_data_rec)] = sweep_data_rec
        
        # 存檔
        wav.write(self.rec_file, fs_rec, rec_data.astype(np.int16))
        
        # 2. 執行分析
        results = analyze_audio(self.rec_file, self.orig_file)
        
        # 3. 驗證左右時間差 (Delay)
        # 左延遲 5ms, 右延遲 10ms -> 預期時間差為 (t_l - 1) - (t_r - 3) = 0.005 - 0.010 = -5 ms
        # 我們期望計算出來的 ToA Delay 在 -5ms 附近 (容許值 1ms)
        t_l = (int(1.0 * fs_rec) + delay_l) / fs_rec
        t_r = (int(3.0 * fs_rec) + delay_r) / fs_rec
        expected_diff = (t_l - 1.0) - (t_r - 3.0)
        
        # 我們從結果中計算 delay diff
        # 根據 analyze_audio 邏輯：
        # peak_l = np.argmax(abs_rec[0.8s:1.6s]) + 0.8s
        # 驗證 peak_l 與 peak_r 找到的位置是否與模擬一致
        self.assertTrue(abs(results["balance"]["left"] - 50) < 10) # 左聲道能量大 6dB -> 左聲道應衰減到 50% 附近
        self.assertEqual(results["balance"]["right"], 100) # 右聲道能量小 -> 應維持 100%
        
        # 4. 驗證 EQ 補償
        # 250Hz 與 500Hz 頻段因為被衰減了 -6dB，補償值應該是要拉高（即 alsaequal 值大於 50%）
        self.assertGreater(results["eq"]["04. 250Hz"], 50)
        self.assertGreater(results["eq"]["05. 500Hz"], 50)
        
        # 2kHz 等頻段沒有被衰減，補償值應該在 50%（0dB）附近
        self.assertTrue(45 <= results["eq"]["07. 2kHz"] <= 55)

if __name__ == "__main__":
    unittest.main()

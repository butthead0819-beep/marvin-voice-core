#!/usr/bin/env python3
"""
room_calibration.py — Mac 大腦端自動空間聲學校正與分析腳本。

自動化流程：
1. 本地生成 sweep.wav 測試音（點擊脈衝測 Balance/ToA + 雙聲道 Log Sweep 測 EQ）
2. 透過 SCP 將測試音與播錄小工具傳送至 Pi，或由本程式以 API 命令 Pi 播錄
3. 將錄好的音檔 record.wav 拉回 Mac
4. 使用 NumPy/SciPy 分析頻率響應，計算 10-band EQ 補償與左右聲道平衡
5. 自動呼叫 Pi 端的 volume_server.py 寫入設定
6. 視覺化頻響圖，儲存至 records/room_response.png
"""
import sys
import os
import time
import argparse
import json
import urllib.request
import subprocess

# 自動嘗試載入或安裝 scipy / numpy / matplotlib
try:
    import numpy as np
    import scipy.io.wavfile as wav
    from scipy.signal import butter, lfilter
except ImportError:
    print("📦 正在為 Mac 大腦端虛擬環境安裝必要的 ASR/DSP 函式庫 (numpy, scipy)...")
    # 使用 venv_simon 中的 pip
    pip_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "venv_simon", "bin", "pip")
    if not os.path.exists(pip_path):
        pip_path = "pip3"
    subprocess.run([pip_path, "install", "numpy", "scipy", "matplotlib"])
    import numpy as np
    import scipy.io.wavfile as wav
    from scipy.signal import butter, lfilter

try:
    import matplotlib.pyplot as plt
    has_plt = True
except ImportError:
    has_plt = False

# 10-band EQ 中心頻率與 ALSA 等化器名稱對應
EQ_BANDS = [
    ("01. 31Hz", 31.25),
    ("02. 63Hz", 62.5),
    ("03. 125Hz", 125.0),
    ("04. 250Hz", 250.0),
    ("05. 500Hz", 500.0),
    ("06. 1kHz", 1000.0),
    ("07. 2kHz", 2000.0),
    ("08. 4kHz", 4000.0),
    ("09. 8kHz", 8000.0),
    ("10. 16kHz", 16000.0),
]

def generate_sweep_file(filename="sweep.wav"):
    """
    生成 48000Hz Stereo 的測試 WAV 檔
    - 1.0s: 左聲道單擊脈衝 (Click)
    - 3.0s: 右聲道單擊脈衝 (Click)
    - 5.0s - 9.0s: 雙聲道 Logarithmic Sweep (20Hz - 20kHz)
    - 總長度: 10.0s
    """
    fs = 48000
    duration = 10.0
    t = np.linspace(0, duration, int(fs * duration), endpoint=False)
    data = np.zeros((len(t), 2), dtype=np.int16)

    # 1.0s 左 Click (脈衝)
    click_idx_l = int(1.0 * fs)
    data[click_idx_l:click_idx_l+100, 0] = 32767

    # 3.0s 右 Click (脈衝)
    click_idx_r = int(3.0 * fs)
    data[click_idx_r:click_idx_r+100, 1] = 32767

    # 5.0s - 9.0s Log Sweep
    sweep_start = int(5.0 * fs)
    sweep_end = int(9.0 * fs)
    T = 4.0
    f0 = 20.0
    f1 = 20000.0
    
    sweep_t = t[sweep_start:sweep_end] - 5.0
    # Log Sweep 數學公式
    sweep_val = np.sin(2 * np.pi * f0 * T / np.log(f1/f0) * ( (f1/f0)**(sweep_t/T) - 1.0 ))
    
    # 漸入漸出 (防爆音)
    fade = int(0.1 * fs)
    window = np.ones_like(sweep_val)
    window[:fade] = np.linspace(0, 1, fade)
    window[-fade:] = np.linspace(1, 0, fade)
    sweep_val = sweep_val * window

    # 轉成 16-bit PCM (保留點餘裕，防 Clipping)
    sweep_pcm = (sweep_val * 28000).astype(np.int16)
    data[sweep_start:sweep_end, 0] = sweep_pcm
    data[sweep_start:sweep_end, 1] = sweep_pcm

    os.makedirs(os.path.dirname(filename) if os.path.dirname(filename) else ".", exist_ok=True)
    wav.write(filename, fs, data)
    print(f"🎵 測試音訊已生成: {filename} (Stereo, 48kHz, 10秒)")

def butter_bandpass(lowcut, highcut, fs, order=3):
    nyq = 0.5 * fs
    low = lowcut / nyq
    high = highcut / nyq
    # 限制頻段在 Nyquist 頻率內，並確保 low < high
    low = max(0.001, min(0.999, low))
    high = max(0.002, min(0.999, high))
    if low >= high:
        low = high - 0.001
        if low <= 0:
            low = 0.001
            high = 0.002
    b, a = butter(order, [low, high], btype='band')
    return b, a

def bandpass_filter(data, lowcut, highcut, fs, order=3):
    b, a = butter_bandpass(lowcut, highcut, fs, order=order)
    y = lfilter(b, a, data)
    return y

def analyze_audio(rec_file, orig_file):
    """
    分析錄音與原始音訊
    1. 計算左右聲道到達時間差 (ToA Delay) 和能量差 (Balance)
    2. 計算 10-band 房間頻率響應並產生補償值
    """
    fs_rec, rec_data = wav.read(rec_file)
    fs_orig, orig_data = wav.read(orig_file)

    # 確保錄音是浮點數，方便計算
    rec_data = rec_data.astype(np.float32)
    # 取絕對值來抓瞬間峰值 (Click)
    abs_rec = np.abs(rec_data)

    # 1. 尋找左 Click 和右 Click 峰值
    # 左 Click 在 1.0s 播出，搜尋區間: 0.8s - 1.6s
    search_l_start = int(0.8 * fs_rec)
    search_l_end = int(1.6 * fs_rec)
    peak_l = np.argmax(abs_rec[search_l_start:search_l_end]) + search_l_start

    # 右 Click 在 3.0s 播出，搜尋區間: 2.8s - 3.6s
    search_r_start = int(2.8 * fs_rec)
    search_r_end = int(3.6 * fs_rec)
    peak_r = np.argmax(abs_rec[search_r_start:search_r_end]) + search_r_start

    # 計算時間差 (ToA)
    # 基準時間差：預期兩者差 2.0 秒
    t_l = peak_l / fs_rec
    t_r = peak_r / fs_rec
    delay_diff = (t_l - 1.0) - (t_r - 3.0) # 秒
    delay_diff_ms = delay_diff * 1000.0

    # 計算能量差 (RMS) 
    # 取 Click 附近 20ms 的視窗
    win = int(0.02 * fs_rec)
    rms_l = np.sqrt(np.mean(rec_data[peak_l-win:peak_l+win]**2)) + 1e-6
    rms_r = np.sqrt(np.mean(rec_data[peak_r-win:peak_r+win]**2)) + 1e-6
    db_diff = 20 * np.log10(rms_l / rms_r)

    # 計算平衡補償 (Balance)
    # 目標：左右能量對齊。衰減較大那一側
    if db_diff > 0.5:
        # 左邊比右邊大：右邊維持 100%，左邊降低
        left_bal = int((10 ** (-db_diff / 20.0)) * 100)
        right_bal = 100
    elif db_diff < -0.5:
        # 右邊比左邊大：左邊維持 100%，右邊降低
        left_bal = 100
        right_bal = int((10 ** (db_diff / 20.0)) * 100)
    else:
        left_bal = 100
        right_bal = 100

    print(f"\n📊 [1. 聲道平衡分析]")
    print(f"   - 左聲道抵達時間: {t_l:.4f}s，右聲道抵達時間: {t_r:.4f}s")
    print(f"   - 左右時間差 (ToA Delay): {delay_diff_ms:.2f} ms")
    print(f"   - 左右能量差: {db_diff:.2f} dB")
    print(f"   - 建議平衡設定 (Balance): 左 {left_bal}%，右 {right_bal}%")

    # 2. 計算 10-band 頻率響應 (Log Sweep 在 5.0s 到 9.0s 播放)
    # 利用 ToA 定位錄音中的掃頻區間 (加上左 Click 的真實延遲)
    real_delay = t_l - 1.0
    sweep_rec_start = int((5.0 + real_delay) * fs_rec)
    sweep_rec_end = int((9.0 + real_delay) * fs_rec)
    rec_sweep = rec_data[sweep_rec_start:sweep_rec_end]

    # 原始 sweep 降採樣到 16000Hz 用於能量對比
    # （我們可以直接計算對應時間段的 sweep 訊號，或直接使用 fs_orig 頻段基準）
    # 這裡我們用帶通濾波器來分析 10 個頻段在錄音中的能量分佈
    rec_eq_db = []
    comp_eq = {}
    
    # alsaequal 預設 50 代表 0dB (不增不減)
    # 我們設定 1dB 變化對應 2.5% 的增益
    # 為了安全防護，補償限制在 [-12dB, +4dB] (等化器限制在 [20%, 60%])
    print(f"\n📊 [2. 頻率響應與等化器補償 (EQ)]")
    
    for band_name, center_freq in EQ_BANDS:
        # 1-octave 寬度的帶通
        low_cut = center_freq / np.sqrt(2)
        high_cut = center_freq * np.sqrt(2)
        
        # 濾波
        filtered_rec = bandpass_filter(rec_sweep, low_cut, high_cut, fs_rec)
        rms_rec = np.sqrt(np.mean(filtered_rec**2)) + 1e-6
        
        # 將能量轉為 dB（相對於 1kHz 的相對頻率響應，拉平音頻曲線）
        rec_eq_db.append(20 * np.log10(rms_rec))

    # 以 1kHz 作為基準點 (0 dB)，計算各頻段與 1kHz 的相對落差
    # 1kHz 是第 5 個頻段 (index 5)
    ref_db = rec_eq_db[5]
    relative_response = [db - ref_db for db in rec_eq_db]
    
    for idx, (band_name, center_freq) in enumerate(EQ_BANDS):
        rel_db = relative_response[idx]
        
        # 補償值 = -相對落差 (反向補償)
        comp_db = -rel_db
        
        # 限幅保護：只允許拉高 4dB，降低 12dB (防低音拉太高燒擴大機)
        comp_db = max(-12.0, min(4.0, comp_db))
        
        # 轉成 alsaequal 百分比 (50% 是 0dB，每 dB 為 2.5%)
        # 例如: +4dB -> 50 + 10 = 60%; -12dB -> 50 - 30 = 20%
        pct_val = 50 + int(comp_db * 2.5)
        pct_val = max(0, min(100, pct_val))
        
        comp_eq[band_name] = pct_val
        print(f"   - {band_name:9s} | 房間響應落差: {rel_db:6.1f} dB | 建議補償: {comp_db:+5.1f} dB -> 套用 EQ 值: {pct_val}%")

    return {
        "balance": {"left": left_bal, "right": right_bal},
        "eq": comp_eq,
        "raw_response": relative_response
    }

def plot_and_save_response(relative_response, save_path="records/room_response.png"):
    if not has_plt:
        return
    plt.figure(figsize=(10, 5))
    freqs = [b[1] for b in EQ_BANDS]
    labels = [b[0].split(". ")[1] for b in EQ_BANDS]
    
    plt.plot(freqs, relative_response, 'o-', color='#ff6b6b', label='Original Room Response')
    # 繪製補償後的預測曲線（理論上拉平到 0dB 附近）
    comp = [-max(-12.0, min(4.0, -r)) for r in relative_response]
    compensated = [r + c for r, c in zip(relative_response, comp)]
    plt.plot(freqs, compensated, 's--', color='#4ec07a', label='Calibrated Prediction')

    plt.xscale('log')
    plt.xticks(freqs, labels)
    plt.grid(True, which="both", ls="--", color='#2a2d38')
    plt.axhline(0, color='#8b90a0', linestyle='-')
    plt.title("Marvin Active Room Calibration - Frequency Response")
    plt.xlabel("Frequency (Hz)")
    plt.ylabel("Relative Gain (dB)")
    plt.legend()
    
    # 套用馬文深色主題視覺
    fig = plt.gcf()
    fig.patch.set_facecolor('#0e0f13')
    ax = plt.gca()
    ax.set_facecolor('#1a1c23')
    ax.spines['bottom'].set_color('#2a2d38')
    ax.spines['top'].set_color('#2a2d38')
    ax.spines['left'].set_color('#2a2d38')
    ax.spines['right'].set_color('#2a2d38')
    ax.xaxis.label.set_color('#e8eaf0')
    ax.yaxis.label.set_color('#e8eaf0')
    ax.title.set_color('#e8eaf0')
    for tick in ax.xaxis.get_major_ticks():
        tick.label1.set_color('#e8eaf0')
    for tick in ax.yaxis.get_major_ticks():
        tick.label1.set_color('#e8eaf0')

    os.makedirs(os.path.dirname(save_path) if os.path.dirname(save_path) else ".", exist_ok=True)
    plt.savefig(save_path, facecolor='#0e0f13')
    plt.close()
    print(f"📊 頻率響應圖已儲存至: {save_path}")

def call_pi_api(ip, port, token, path, payload):
    url = f"http://{ip}:{port}{path}?t={token}"
    data_bytes = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=data_bytes,
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            res_body = response.read().decode('utf-8')
            return json.loads(res_body)
    except Exception as e:
        print(f"❌ 呼叫 Pi API 失敗 ({path}): {e}")
        return None

def main():
    parser = argparse.ArgumentParser(description="Marvin 自動空間聲學校正工具")
    parser.add_argument("--pi-ip", required=True, help="Pi 的 IP 位址")
    parser.add_argument("--port", type=int, default=8766, help="volume_server.py 的埠號")
    parser.add_argument("--token", default="", help="API Token")
    parser.add_argument("--rec-dev", default="plughw:CARD=XVF3800,DEV=0", help="Pi 端錄音裝置")
    parser.add_argument("--play-dev", default="default", help="Pi 端播放裝置")
    parser.add_argument("--local-only", action="store_true", help="僅進行本地 DSP 分析，不進行實機播錄與部署")
    args = parser.parse_args()

    orig_wav = "assets/sweep.wav"
    rec_wav = "records/calibration_record.wav"

    # 生成 sweep 測試音
    generate_sweep_file(orig_wav)

    if args.local_only:
        if not os.path.exists(rec_wav):
            print(f"❌ 本地分析模式需要存在 {rec_wav}")
            return
        results = analyze_audio(rec_wav, orig_wav)
        plot_and_save_response(results["raw_response"])
        return

    # 1. 將測試音 sweep.wav 傳送到 Pi
    print(f"📤 傳送 sweep.wav 測試音至 Pi...")
    # 使用 scp
    scp_cmd = ["scp", orig_wav, f"pi@{args.pi_ip}:/tmp/sweep.wav"]
    print(f"   執行: {' '.join(scp_cmd)}")
    subprocess.run(scp_cmd, check=True)

    # 2. 將播錄小工具傳送至 Pi
    helper_script = "device/play_record_helper.py"
    print(f"📤 傳送播錄小工具至 Pi...")
    scp_cmd2 = ["scp", helper_script, f"pi@{args.pi_ip}:/tmp/play_record_helper.py"]
    subprocess.run(scp_cmd2, check=True)

    # 3. 遙控 Pi 進行本地同步播放與錄音
    print(f"🎤 遙控 Pi 啟動同步播錄中，請保持室內絕對安靜... (大約需要 15 秒)")
    # 使用 ssh 遠端執行 play_record_helper.py
    ssh_cmd = [
        "ssh", f"pi@{args.pi_ip}",
        f"python3 /tmp/play_record_helper.py --play /tmp/sweep.wav --record /tmp/record.wav --play-dev {args.play_dev} --rec-dev {args.rec_dev} --duration 12"
    ]
    print(f"   執行: {' '.join(ssh_cmd)}")
    subprocess.run(ssh_cmd, check=True)

    # 4. 將錄音拉回 Mac
    print(f"📥 將 Pi 端的錄音檔拉回 Mac 大腦...")
    scp_back_cmd = ["scp", f"pi@{args.pi_ip}:/tmp/record.wav", rec_wav]
    subprocess.run(scp_back_cmd, check=True)

    # 5. 聲學 DSP 分析
    print(f"🧮 開始計算聲學補償參數...")
    results = analyze_audio(rec_wav, orig_wav)
    plot_and_save_response(results["raw_response"])

    # 6. 自動套用設定至 Pi 端的 volume_server
    print(f"\n🚀 自動將最佳化設定部署到 Pi...")
    
    # 寫入 Balance
    res_bal = call_pi_api(args.pi_ip, args.port, args.token, "/balance", results["balance"])
    if res_bal and res_bal.get("ok"):
        print("   ✅ 左右聲道平衡 Balance 設定成功！")
    
    # 寫入 EQ
    res_eq = call_pi_api(args.pi_ip, args.port, args.token, "/eq", results["eq"])
    if res_eq and res_eq.get("ok"):
        print("   ✅ 10-band 等化器 (EQ) 部署成功！")

    print("\n🎉 [空間聲學校正完成] 馬文音箱已調整至客廳最佳狀態！")

if __name__ == "__main__":
    main()

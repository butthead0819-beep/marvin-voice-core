#!/usr/bin/env python3
"""
play_record_helper.py — Pi 邊緣端本地同步播錄小工具。

避免網路延遲與抖動（Jitter）干擾聲學延遲（ToA）與平衡的計算。
本工具直接調用 ALSA 的 aplay 與 arecord。在本地端近乎同時啟動。
"""
import argparse
import subprocess
import threading
import time
import os

def run_cmd(cmd, name):
    print(f"[{name}] 啟動: {' '.join(cmd)}")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"[{name}] 失敗 (code {res.returncode}): {res.stderr}")
    else:
        print(f"[{name}] 完成")

def main():
    parser = argparse.ArgumentParser(description="Marvin 聲學校正 - 本地同步播錄小工具")
    parser.add_argument("--play", required=True, help="播放的 WAV 檔案路徑")
    parser.add_argument("--record", required=True, help="錄音儲存的 WAV 檔案路徑")
    parser.add_argument("--play-dev", default="default", help="播放 ALSA 裝置")
    parser.add_argument("--rec-dev", required=True, help="錄音 ALSA 裝置")
    parser.add_argument("--rec-rate", type=int, default=16000, help="錄音採樣率")
    parser.add_argument("--rec-channels", type=int, default=1, help="錄音通道數")
    parser.add_argument("--rec-format", default="S16_LE", help="錄音格式")
    parser.add_argument("--duration", type=float, default=12.0, help="錄音時間(秒)")
    args = parser.parse_args()

    if not os.path.exists(args.play):
        print(f"❌ 錯誤: 找不到播放檔案 {args.play}")
        return

    # arecord 錄音命令 (不帶 -d 參數，由 Python 主動控制時間)
    rec_cmd = [
        "arecord",
        "-D", args.rec_dev,
        "-r", str(args.rec_rate),
        "-c", str(args.rec_channels),
        "-f", args.rec_format,
        args.record
    ]

    # aplay 播放命令
    play_cmd = [
        "aplay",
        "-D", args.play_dev,
        args.play
    ]

    print("📢 [Acoustic Calibration Helper] 開始同步播錄...")
    
    # 啟動錄音
    print(f"[Recorder] 啟動: {' '.join(rec_cmd)}")
    rec_proc = subprocess.Popen(rec_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    
    # 暖機 0.3 秒以確保錄音 Buffer 備妥，不錯失 Click 音
    time.sleep(0.3)
    
    # 啟動播放
    print(f"[Player] 啟動: {' '.join(play_cmd)}")
    play_proc = subprocess.Popen(play_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    # 等待指定的錄音持續時間
    time.sleep(args.duration)

    print("📢 [Acoustic Calibration Helper] 時間已到，終止播錄進程...")
    try:
        play_proc.terminate()
        rec_proc.terminate()
        play_proc.wait(timeout=2)
        rec_proc.wait(timeout=2)
    except Exception as e:
        print(f"⚠️ 終止進程時遇到異常: {e}")
        try:
            play_proc.kill()
            rec_proc.kill()
        except Exception:
            pass

    # 檢查產生的音檔大小
    # 等待一點點時間讓檔案寫入完成
    time.sleep(0.5)

    if os.path.exists(args.record) and os.path.getsize(args.record) > 44:
        print(f"✅ 播錄成功，錄音檔已存至: {args.record} ({os.path.getsize(args.record)} bytes)")
    else:
        print("❌ 播錄失敗，錄音檔未生成或大小為空。")

if __name__ == "__main__":
    main()

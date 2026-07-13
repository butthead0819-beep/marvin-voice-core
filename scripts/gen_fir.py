#!/usr/bin/env python3
"""
gen_fir.py — 從 10-band EQ profile 產生 FIR 衝激響應 WAV
供 shairport-sync convolution filter 使用。

用法:
  python3 scripts/gen_fir.py --profile room_calibrated --push-to-pi
  python3 scripts/gen_fir.py --profile extreme_bass --push-to-pi
"""
import argparse
import json
import os
import subprocess
import sys

import numpy as np
import scipy.signal as sig
import scipy.io.wavfile as wav

BANDS_HZ = [31, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
FS = 44100
TAPS = 4096


def eq_percent_to_db(pct):
    return (float(pct) - 50.0) / 50.0 * 12.0


def generate_fir(band_gains_db, fs=FS, taps=TAPS):
    # firwin2 要求奇數 tap 數 (Type I)，或 Nyquist 增益為 0 (Type II)
    if taps % 2 == 0:
        taps += 1

    freqs = [0.0] + [f / (fs / 2) for f in BANDS_HZ] + [1.0]
    gains_linear = [1.0]
    for g in band_gains_db:
        gains_linear.append(10 ** (g / 20.0))
    gains_linear.append(0.0)  # Nyquist 強制歸零（Type I 奇數可任意，但明確設 0 最安全）

    fir = sig.firwin2(taps, freqs, gains_linear, window="hann")
    max_val = np.max(np.abs(fir))
    if max_val > 0:
        fir = fir / max_val * 0.9
    return fir.astype(np.float32)


def add_reflection(fir, delay_ms, gain, fs=44100):
    delay_samples = int(delay_ms * fs / 1000.0)
    out = np.copy(fir)
    if delay_samples < len(fir):
        out[delay_samples:] += gain * fir[:-delay_samples]
    return out


def fetch_profile_from_pi(pi_ip, profile_name, token):
    import urllib.request
    url = f"http://{pi_ip}:8766/profile?t={token}"
    with urllib.request.urlopen(url, timeout=5) as r:
        data = json.loads(r.read())
    profiles = data.get("current_profiles", {})
    if profile_name not in profiles:
        raise ValueError(f"Profile '{profile_name}' not found. Available: {list(profiles.keys())}")
    return profiles[profile_name]


def profile_to_gains(profile, profile_name=""):
    result = []
    values = list(profile.values())
    is_extreme = "extreme" in profile_name.lower()
    scale = 35.0 if is_extreme else 12.0
    for i, band_hz in enumerate(BANDS_HZ):
        matched = None
        for k, v in profile.items():
            k_clean = k.lower().replace(" ", "").replace(".", "")
            hz_str = str(band_hz)
            if hz_str + "hz" in k_clean or k_clean.endswith(hz_str):
                matched = v
                break
        if matched is None:
            matched = values[i] if i < len(values) else 50
        db = (float(matched) - 50.0) / 50.0 * scale
        result.append(db)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="room_calibrated")
    parser.add_argument("--pi-ip", default="100.121.35.41")
    parser.add_argument("--token", default=os.getenv("MARVIN_TEXT_TOKEN", ""))
    parser.add_argument("--eq", default=None)
    parser.add_argument("--out", default="assets/eq_fir.wav")
    parser.add_argument("--push-to-pi", action="store_true")
    parser.add_argument("--taps", type=int, default=TAPS)
    args = parser.parse_args()

    if args.eq:
        profile = json.loads(args.eq)
    else:
        print(f"📡 從 Pi ({args.pi_ip}) 拉取 Profile: {args.profile} ...")
        try:
            profile = fetch_profile_from_pi(args.pi_ip, args.profile, args.token)
        except Exception as e:
            print(f"❌ 無法連接 Pi API: {e}")
            sys.exit(1)

    gains_db = profile_to_gains(profile, args.profile)
    print(f"🎚️  EQ 增益 (dB): {[f'{g:+.1f}' for g in gains_db]}")

    print(f"🔧 產生 FIR 濾波器（{args.taps} taps, {FS}Hz）...")
    fir = generate_fir(gains_db, fs=FS, taps=args.taps)

    if args.profile.lower() == "spatial":
        print("🌟 套用空間環繞 (Spatial) 早期反射與相位差音效演算法...")
        # 左聲道反射：12.0ms 與 26.0ms (反相) 延遲反射
        fir_l = add_reflection(fir, 12.0, 0.25, fs=FS)
        fir_l = add_reflection(fir_l, 26.0, -0.15, fs=FS)
        # 右聲道反射：16.0ms (反相) 與 21.0ms 延遲反射
        fir_r = add_reflection(fir, 16.0, -0.25, fs=FS)
        fir_r = add_reflection(fir_r, 21.0, 0.15, fs=FS)
    else:
        fir_l = fir
        fir_r = fir

    # 統一進行雙聲道最大振幅正規化，防止數位失真
    max_val = max(np.max(np.abs(fir_l)), np.max(np.abs(fir_r)))
    if max_val > 0:
        fir_l = fir_l / max_val * 0.9
        fir_r = fir_r / max_val * 0.9

    # Convert to 16-bit signed PCM (int16) for maximum libsndfile compatibility
    fir_l_int16 = (fir_l * 32767.0).astype(np.int16)
    fir_r_int16 = (fir_r * 32767.0).astype(np.int16)
    fir_stereo = np.column_stack([fir_l_int16, fir_r_int16])
    os.makedirs(os.path.dirname(args.out) if os.path.dirname(args.out) else ".", exist_ok=True)
    wav.write(args.out, FS, fir_stereo)
    print(f"✅ 16-bit PCM Stereo FIR WAV 已儲存: {args.out}  ({os.path.getsize(args.out)//1024} KB)")

    if args.push_to_pi:
        pi_path = "/etc/marvin-device/eq_fir.wav"
        print(f"📤 SCP 傳送到 Pi: {pi_path} ...")
        subprocess.run(["scp", args.out, f"pi@{args.pi_ip}:{pi_path}"], check=True)
        print(f"🔄 重載 shairport-sync ...")
        subprocess.run(["ssh", f"pi@{args.pi_ip}",
            "sudo kill -SIGTERM $(pidof shairport-sync) 2>/dev/null; sleep 1; sudo systemctl restart shairport-sync"],
            check=False)
        print(f"🎉 AirPlay EQ 已套用！")


if __name__ == "__main__":
    main()

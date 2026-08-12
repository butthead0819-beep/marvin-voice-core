#!/usr/bin/env python3
"""
合成 DJ Tail 轉場音效（scratch / air horn / riser），純 numpy/scipy 產生、無外部素材授權疑慮。
每支 <2s，供 _run_tail_dj 在 DJ 口白播完後疊入下一首開頭。
Run from repo root: python scripts/gen_dj_sfx.py
"""
import os
import random
import wave

import numpy as np
from scipy.signal import butter, sosfilt

RATE = 44100
SFX_DIR = "assets/dj_sfx"


def _write_wav(path: str, samples: np.ndarray) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pcm16 = np.clip(samples, -1.0, 1.0)
    pcm16 = (pcm16 * 32767).astype(np.int16)
    with wave.open(path, "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(RATE)
        f.writeframes(pcm16.tobytes())


def _fade(samples: np.ndarray, attack: float = 0.005, release: float = 0.03) -> np.ndarray:
    n = len(samples)
    att = int(RATE * attack)
    rel = int(RATE * release)
    out = samples.copy()
    if att > 0:
        out[:att] *= np.linspace(0, 1, att)
    if rel > 0:
        out[n - rel:] *= np.linspace(1, 0, rel)
    return out


def _sinpow(x: np.ndarray, power: float) -> np.ndarray:
    """sin(...) 在邊界(tn≈0 或 1) 因浮點誤差可能落到 -1e-16 等極小負值，非整數次方
    對負底數會產生 NaN。clip 到 0 再取次方——量級上跟正常訊號差 16 個數量級，聽感
    上截掉的只有數值雜訊，不是真正的訊號。"""
    return np.clip(x, 0.0, None) ** power


def find_cue_point(raw_pcm: np.ndarray, rate: int = RATE, search_window_s: float = 2.0) -> float:
    """尋找歌曲前奏中最具代表性、最清晰的音訊起點 (Cue Point / Transient Attack)。

    避免在開頭 0~0.8s 的靜音或微弱雜音上刷碟，而是對準第一個鼓點/人聲/重音攻擊音。
    """
    if raw_pcm.ndim > 1:
        raw_pcm = np.mean(raw_pcm, axis=-1)

    max_search = min(len(raw_pcm), int(rate * search_window_s))
    if max_search < int(rate * 0.1):
        return 0.20

    search_pcm = raw_pcm[:max_search]
    win_size = int(rate * 0.005)   # 5ms 窗口
    hop_size = int(rate * 0.0025)  # 2.5ms 步進
    num_frames = (len(search_pcm) - win_size) // hop_size

    if num_frames <= 0:
        return 0.20

    energies = np.array([
        np.sqrt(np.mean(search_pcm[i * hop_size : i * hop_size + win_size] ** 2))
        for i in range(num_frames)
    ])

    max_e = float(np.max(energies)) if len(energies) else 0.0
    if max_e < 1e-4:
        return 0.20  # 整段極靜音

    threshold = max(0.04, max_e * 0.20)
    candidates = np.where(energies >= threshold)[0]
    if len(candidates) > 0:
        cue_idx = candidates[0] * hop_size
        cue_s = cue_idx / rate
        # 稍微往前保留 20ms 的 attack 前緣
        return max(0.04, cue_s - 0.02)
    return 0.25


SCRATCH_STYLES = ("wicka", "spinback", "vinyl_brake", "chirp", "transform", "tear")

_STYLE_DURATIONS = {
    "wicka": 0.65,
    "spinback": 0.70,
    "vinyl_brake": 0.75,
    "chirp": 0.55,
    "transform": 0.65,
    "tear": 0.60,
}


def _calc_scratch_trajectory(style: str | None, dur: float | None, rate: int,
                             bpm: float | None = None) -> tuple[np.ndarray, np.ndarray, float, float, float]:
    """計算連續單一轉盤速度軌跡 v(t)、Crossfader 門閥 A(t)、Cue 點微調位移、摩擦增益、Rumble增益。"""
    if bpm and bpm > 0:
        beat = 60.0 / float(bpm)
        # 控制總長在 1.0 ~ 1.7 秒之間
        if beat * 3 <= 1.7:
            dur = beat * 3
        elif beat * 2 <= 1.7:
            dur = beat * 2
        else:
            dur = min(1.7, max(1.0, beat * 2))
            beat = dur / 2.0
    else:
        if dur is None or dur <= 0:
            dur = _STYLE_DURATIONS.get(style or "wicka", 0.65)

    n = int(rate * dur)
    t = np.linspace(0, dur, n)
    v = np.zeros(n)
    fader = np.ones(n)
    cue_offset_s = 0.0
    fric_gain = 0.06
    rumble_gain = 0.03

    if style is None or style not in SCRATCH_STYLES:
        style = random.choice(SCRATCH_STYLES)

    if bpm and bpm > 0:
        beat = 60.0 / float(bpm)
        # ── BPM-Synced 連貫 Turntablism Routine ──
        if style == "spinback":
            # 1 拍 Baby 對位暖身 + 指數衰減急速倒盤回甩
            t_setup = min(0.45, dur * 0.35)
            seg1 = (t < t_setup)
            v[seg1] = 2.8 * np.sin(2 * np.pi * t[seg1] / (t_setup / 2.0))
            fader[seg1] = 1.0

            seg2 = (t >= t_setup)
            t_spin = dur - t_setup
            t_rel = t[seg2] - t_setup
            v[seg2] = -8.5 * np.exp(-3.8 * t_rel) * (np.maximum(0.0, 1.0 - t_rel / t_spin) ** 0.5)
            fader[seg2] = np.maximum(0.0, 1.0 - (t_rel / t_spin) ** 1.8)
            cue_offset_s = 0.25
            fric_gain = 0.08
            rumble_gain = 0.03

        elif style == "vinyl_brake":
            # 短暫正常播放 + 平滑斷電煞車降速
            t_norm = min(0.20, dur * 0.20)
            seg1 = (t < t_norm)
            v[seg1] = 1.0
            fader[seg1] = 1.0

            seg2 = (t >= t_norm)
            t_brake = dur - t_norm
            t_rel = t[seg2] - t_norm
            v[seg2] = np.maximum(0.0, 1.0 - t_rel / t_brake) ** 1.5
            fader[seg2] = np.maximum(0.0, 1.0 - (t_rel / t_brake) ** 1.2)
            cue_offset_s = 0.0
            fric_gain = 0.04
            rumble_gain = 0.06

        elif style == "chirp":
            # 16 分音符清脆鳥鳴刷 + 結尾 Drop 放盤
            t_chirp_end = dur * 0.75
            seg_c = (t < t_chirp_end)
            t_16 = max(0.08, beat / 4.0)
            phase_c = (t[seg_c] % t_16) / t_16
            v[seg_c] = np.where(phase_c < 0.5, 3.8 * np.sin(np.pi * phase_c / 0.5), -3.8 * np.sin(np.pi * (phase_c - 0.5) / 0.5))
            spd_abs = np.abs(v[seg_c])
            fader[seg_c] = np.clip((spd_abs - 1.2) / 1.5, 0.0, 1.0) ** 1.2

            seg_drop = (t >= t_chirp_end)
            t_rel = (t[seg_drop] - t_chirp_end) / max(1e-4, dur - t_chirp_end)
            v[seg_drop] = np.where(t_rel < 0.3, -2.5 * np.sin(np.pi * t_rel / 0.3), 1.0)
            fader[seg_drop] = 1.0
            cue_offset_s = 0.0
            fric_gain = 0.06
            rumble_gain = 0.03

        elif style == "transform":
            # 平滑往復運動 + 16 分音符方形門閥切音 (Machine Gun Gate) + Drop
            t_trans_end = dur * 0.75
            seg_t = (t < t_trans_end)
            v[seg_t] = 2.2 * np.sin(2 * np.pi * t[seg_t] / beat)
            t_16 = max(0.08, beat / 4.0)
            phase_g = (t[seg_t] % t_16) / t_16
            fader[seg_t] = np.where(phase_g < 0.55, 1.0, 0.0)

            seg_drop = (t >= t_trans_end)
            t_rel = (t[seg_drop] - t_trans_end) / max(1e-4, dur - t_trans_end)
            v[seg_drop] = np.where(t_rel < 0.3, -2.0 * np.sin(np.pi * t_rel / 0.3), 1.0)
            fader[seg_drop] = 1.0
            cue_offset_s = 0.0
            fric_gain = 0.05
            rumble_gain = 0.03

        elif style == "tear":
            # 雙速前推撕裂刷 (Tear) + 結尾 Drop
            t_tear_end = dur * 0.75
            seg_tear = (t < t_tear_end)
            t_8 = max(0.15, beat / 2.0)
            phase_tear = (t[seg_tear] % t_8) / t_8
            v[seg_tear] = np.where(
                phase_tear < 0.25,
                2.2 * np.sin(np.pi * phase_tear / 0.25),
                np.where(
                    phase_tear < 0.5,
                    4.2 * np.sin(np.pi * (phase_tear - 0.25) / 0.25),
                    -3.6 * np.sin(np.pi * (phase_tear - 0.5) / 0.5)
                )
            )
            fader[seg_tear] = 1.0

            seg_drop = (t >= t_tear_end)
            t_rel = (t[seg_drop] - t_tear_end) / max(1e-4, dur - t_tear_end)
            v[seg_drop] = np.where(t_rel < 0.3, -2.5 * np.sin(np.pi * t_rel / 0.3), 1.0)
            fader[seg_drop] = 1.0
            cue_offset_s = 0.0
            fric_gain = 0.06
            rumble_gain = 0.03

        else:  # "wicka"
            # 經典 4-stroke Wicka (2 Baby + 2 Forward Cuts + Drop)
            t_b1 = min(beat, dur * 0.38)
            seg1 = (t < t_b1)
            v[seg1] = 3.0 * np.sin(2 * np.pi * t[seg1] / (t_b1 / 2.0))
            fader[seg1] = 1.0

            t_b2 = min(2 * beat, dur * 0.75)
            seg2 = (t >= t_b1) & (t < t_b2)
            t_seg2 = t[seg2] - t_b1
            t_cut = max(0.12, (t_b2 - t_b1) / 2.0)
            phase_cut = (t_seg2 % t_cut) / t_cut
            v[seg2] = np.where(phase_cut < 0.5, 3.5 * np.sin(np.pi * phase_cut / 0.5), -4.0 * np.sin(np.pi * (phase_cut - 0.5) / 0.5))
            fader[seg2] = np.where(phase_cut < 0.5, 1.0, 0.0)

            seg3 = (t >= t_b2)
            t_rel = (t[seg3] - t_b2) / max(1e-4, dur - t_b2)
            v[seg3] = np.where(t_rel < 0.25, -2.5 * np.sin(np.pi * t_rel / 0.25), 1.0)
            fader[seg3] = 1.0
            cue_offset_s = 0.0
            fric_gain = 0.06
            rumble_gain = 0.03

    else:
        # ── 單一手勢手動微調模式 ──
        if style == "spinback":
            v = -8.5 * np.exp(-3.8 * t) * (np.maximum(0.0, 1.0 - t / dur) ** 0.5)
            fader = np.maximum(0.0, 1.0 - (t / dur) ** 1.8)
            cue_offset_s = 0.25
            fric_gain = 0.08
            rumble_gain = 0.03

        elif style == "vinyl_brake":
            v = (np.maximum(0.0, 1.0 - t / dur)) ** 1.5
            fader = np.maximum(0.0, 1.0 - (t / dur) ** 1.2)
            cue_offset_s = 0.0
            fric_gain = 0.04
            rumble_gain = 0.06

        elif style == "chirp":
            cuts = [f * dur for f in (0.0, 0.25, 0.50, 0.75, 1.0)]
            for i in range(4):
                t_s, t_e = cuts[i], cuts[i + 1]
                seg = (t >= t_s) & (t < t_e)
                if not np.any(seg):
                    continue
                tn = (t[seg] - t_s) / (t_e - t_s)
                spd = [4.2, -4.2, 4.5, -4.0][i]
                v[seg] = spd * _sinpow(np.sin(np.pi * tn), 1.2)
            spd_abs = np.abs(v)
            fader = np.clip((spd_abs - 1.2) / 1.5, 0.0, 1.0) ** 1.2
            cue_offset_s = 0.0
            fric_gain = 0.06
            rumble_gain = 0.03

        elif style == "transform":
            v = 2.2 * np.sin(2 * np.pi * t / dur)
            gate = np.sin(2 * np.pi * 14.0 * t)
            fader = np.where(gate > 0.0, 1.0, 0.0)
            cue_offset_s = 0.0
            fric_gain = 0.05
            rumble_gain = 0.03

        elif style == "tear":
            cuts = [f * dur for f in (0.0, 0.2, 0.45, 0.65, 1.0)]
            s1 = (t >= cuts[0]) & (t < cuts[1])
            v[s1] = 2.0 * np.sin(np.pi * (t[s1] - cuts[0]) / (cuts[1] - cuts[0]))
            s2 = (t >= cuts[1]) & (t < cuts[2])
            v[s2] = 4.2 * _sinpow(np.sin(np.pi * (t[s2] - cuts[1]) / (cuts[2] - cuts[1])), 1.2)
            s3 = (t >= cuts[2]) & (t < cuts[3])
            v[s3] = -1.8 * np.sin(np.pi * (t[s3] - cuts[2]) / (cuts[3] - cuts[2]))
            s4 = (t >= cuts[3]) & (t <= cuts[4])
            v[s4] = -4.0 * _sinpow(np.sin(np.pi * (t[s4] - cuts[3]) / (cuts[4] - cuts[3])), 1.2)
            fader = np.ones(n)
            cue_offset_s = 0.0
            fric_gain = 0.06
            rumble_gain = 0.03

        else:  # "wicka"
            cuts = [f * dur for f in (0.0, 0.2, 0.4, 0.6, 1.0)]
            s1 = (t >= cuts[0]) & (t < cuts[1])
            v[s1] = -2.8 * _sinpow(np.sin(np.pi * (t[s1] - cuts[0]) / (cuts[1] - cuts[0])), 1.2)
            s2 = (t >= cuts[1]) & (t < cuts[2])
            v[s2] = 3.2 * _sinpow(np.sin(np.pi * (t[s2] - cuts[1]) / (cuts[2] - cuts[1])), 1.2)
            s3 = (t >= cuts[2]) & (t < cuts[3])
            v[s3] = -3.5 * _sinpow(np.sin(np.pi * (t[s3] - cuts[2]) / (cuts[3] - cuts[2])), 1.2)
            s4 = (t >= cuts[3]) & (t <= cuts[4])
            v[s4] = 3.8 * np.sin(np.pi * (t[s4] - cuts[3]) / (cuts[4] - cuts[3]) * 0.75) * np.exp(-2.5 * (t[s4] - cuts[3]) / (cuts[4] - cuts[3]))
            fader = np.ones(n)
            cue_offset_s = 0.0
            fric_gain = 0.06
            rumble_gain = 0.03

    return v, fader, cue_offset_s, fric_gain, rumble_gain


def _calc_scratch_profile(style: str, dur: float, rate: int) -> tuple[np.ndarray, np.ndarray, float, float, float]:
    """舊版相容性包裝：計算指定刷碟手法的速度曲線 v(t)、Crossfader 門閥 A(t)、基準原點 p0、摩擦增益、Rumble增益。"""
    v, fader, _offset, fric_gain, rumble_gain = _calc_scratch_trajectory(style, dur, rate, bpm=None)
    return v, fader, 0.8, fric_gain, rumble_gain


def gen_scratch_from_pcm(raw_pcm: np.ndarray, rate: int = RATE, style: str | None = None,
                         bpm: float | None = None) -> np.ndarray:
    """使用傳入的音樂 PCM 進行真實 DJ 黑膠轉盤手刷調變。

    raw_pcm: float32 PCM（單聲道或雙聲道均可）
    rate: 採樣率（預設 44100，或 48000）
    style: 手法風格名稱（"wicka", "spinback", "vinyl_brake", "chirp", "transform", "tear"）。
           若為 None 則隨機挑選。
    bpm: 下一首歌的 BPM。給定時生成與小節節奏嚴密對齊的連貫 Turntablism Routine（總長 1.0~1.7s），
         帶有平滑放盤切入；未給定時生成單一手勢（時長 0.55~0.75s）。
    """
    if raw_pcm.ndim > 1:
        raw_pcm = np.mean(raw_pcm, axis=-1)

    raw_pcm = raw_pcm.astype(np.float32)

    # 1. 統一正規化輸入峰值至 0.90，消除安靜歌曲與大音量歌曲間的動態失衡
    peak = float(np.max(np.abs(raw_pcm))) if raw_pcm.size else 0.0
    if peak > 1e-6:
        raw_pcm = raw_pcm * (0.90 / peak)

    # 2. 保障最少 4 秒緩衝區供唱針大幅度前後移動
    min_samples = int(rate * 4.0)
    if len(raw_pcm) < min_samples:
        repeats = int(np.ceil(min_samples / max(1, len(raw_pcm))))
        raw_pcm = np.tile(raw_pcm, repeats)[:min_samples]

    # 3. 動態尋找音樂前奏的第一個 Attack / 重音點 (Cue Point)
    cue_point = find_cue_point(raw_pcm, rate=rate)

    # 4. 生成連續平滑的速度與 Crossfader 門閥軌跡
    v, fader, cue_offset_s, fric_gain, rumble_gain = _calc_scratch_trajectory(
        style=style, dur=None, rate=rate, bpm=bpm
    )
    n = len(v)

    # 5. 連續物理位移積分 x(t) = p0 + ∫ v(t) dt
    start_pos = max(0.05, cue_point + cue_offset_s)
    pos = start_pos + np.cumsum(v) / rate
    pos_idx = np.clip(pos * rate, 0, len(raw_pcm) - 2)
    idx_f = pos_idx.astype(int)
    idx_frac = pos_idx - idx_f
    scratched_tone = (1.0 - idx_frac) * raw_pcm[idx_f] + idx_frac * raw_pcm[idx_f + 1]

    # 6. Crossfader 門閥施加 2ms 微小平滑濾波，消除方波硬切產生的數位喀噠聲 (Anti-Click)
    sos_fader = butter(1, min(600, int(rate * 0.45)), btype="lowpass", fs=rate, output="sos")
    fader_smooth = np.clip(sosfilt(sos_fader, fader), 0.0, 1.0)

    # 7. 電磁唱頭動態感應音量曲線：速度越快聲音越響亮，停滯點 (v=0) 靜音
    speed = np.abs(v)
    speed_env = np.clip(speed ** 0.7, 0.0, 1.25)
    vol_env = speed_env * fader_smooth

    # 8. 唱針微觀摩擦層 (Needle Friction) 與唱盤箱體低頻 (Turntable Rumble)
    rng = np.random.default_rng(2026)
    noise = rng.normal(0, 1, n)
    sos_fric = butter(2, [1200, min(5500, int(rate * 0.45))], btype="bandpass", fs=rate, output="sos")
    friction = sosfilt(sos_fric, noise) * fric_gain * (speed ** 0.8)

    sos_rumble = butter(2, [45, 95], btype="bandpass", fs=rate, output="sos")
    rumble = sosfilt(sos_rumble, rng.normal(0, 1, n)) * rumble_gain * (speed ** 0.4)

    # 9. 混音組合：音樂本體佔 92%，物理表面質感佔 8%
    mix = (scratched_tone * 0.92 + friction + rumble) * vol_env

    # 10. 類比溫暖度濾波（抑制超高頻數位 Aliasing，留下扎實黑膠感）
    sos_body = butter(2, min(8000, int(rate * 0.45)), btype="lowpass", fs=rate, output="sos")
    mix = sosfilt(sos_body, mix)

    # 11. 類比軟飽和 (Analog Saturation)
    out = np.tanh(mix * 1.35)

    return _fade(out.astype(np.float32), attack=0.005, release=0.03)


def gen_scratch() -> np.ndarray:
    """刷碟：使用內建經典黑膠 Formant 切片合成真實 DJ 黑膠轉盤音效。"""
    sample_dur = 4.0
    st = np.arange(int(RATE * sample_dur)) / RATE
    f0 = 170.0
    saw = 2 * (st * f0 - np.floor(0.5 + st * f0))
    pulse = np.where((st * f0 % 1.0) < 0.32, 1.0, -1.0)
    source_wave = 0.55 * saw + 0.45 * pulse

    # 經典嘻哈 "Ahhh/Fresh" 3 階共振峰濾波
    sos_f1 = butter(2, [580, 820], btype="bandpass", fs=RATE, output="sos")
    sos_f2 = butter(2, [1150, 1550], btype="bandpass", fs=RATE, output="sos")
    sos_f3 = butter(2, [2350, 3100], btype="bandpass", fs=RATE, output="sos")
    f1 = sosfilt(sos_f1, source_wave) * 1.7
    f2 = sosfilt(sos_f2, source_wave) * 1.3
    f3 = sosfilt(sos_f3, source_wave) * 0.9
    vinyl_content = np.tanh((f1 + f2 + f3) * 1.7)

    return gen_scratch_from_pcm(vinyl_content, rate=RATE, style="wicka")


def gen_dj_airhorn() -> np.ndarray:
    """雷鬼 DJ 空氣號角「meep-meep」兩響，比 assets/sfx/air_horn.wav 更短促、堆疊泛音更厚。"""
    def hit(dur: float) -> np.ndarray:
        n = int(RATE * dur)
        t = np.arange(n) / RATE
        # 基頻 + 完全五度堆疊，方波製造喇叭感
        sig = 0.5 * np.sign(np.sin(2 * np.pi * 370 * t))
        sig += 0.35 * np.sign(np.sin(2 * np.pi * 554 * t))
        sig += 0.15 * np.sign(np.sin(2 * np.pi * 740 * t))
        return _fade(sig, attack=0.01, release=0.05)

    gap = np.zeros(int(RATE * 0.08))
    return np.concatenate([hit(0.32), gap, hit(0.32)])


def gen_riser() -> np.ndarray:
    """上升 riser／whoosh：噪點淡入疊加快速上滑音，轉場前墊一下氣氛。"""
    dur = 1.1
    n = int(RATE * dur)
    t = np.arange(n) / RATE
    sweep_freq = 200 + (2200 - 200) * (t / dur) ** 2
    phase = 2 * np.pi * np.cumsum(sweep_freq) / RATE
    tone = np.sin(phase)
    noise = np.random.default_rng(1).uniform(-1, 1, n)
    env = (t / dur) ** 1.5
    sig = (0.5 * tone + 0.5 * noise) * env
    return _fade(sig, attack=0.02, release=0.03)


SOUNDS = {
    "scratch": gen_scratch,
    "dj_airhorn": gen_dj_airhorn,
    "riser": gen_riser,
}

if __name__ == "__main__":
    os.makedirs(SFX_DIR, exist_ok=True)
    for name, fn in SOUNDS.items():
        path = f"{SFX_DIR}/{name}.wav"
        samples = fn()
        _write_wav(path, samples)
        print(f"✅  {path}  ({len(samples) / RATE:.2f}s)")
    print("Done.")

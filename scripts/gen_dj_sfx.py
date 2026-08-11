#!/usr/bin/env python3
"""
合成 DJ Tail 轉場音效（scratch / air horn / riser），純 numpy 產生、無外部素材授權疑慮。
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


def _fade(samples: np.ndarray, attack: float = 0.01, release: float = 0.08) -> np.ndarray:
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


SCRATCH_STYLES = ("wicka", "spinback", "vinyl_brake", "chirp", "transform", "tear")

_STYLE_DURATIONS = {
    "wicka": 0.65,
    "spinback": 0.70,
    "vinyl_brake": 0.75,
    "chirp": 0.52,
    "transform": 0.65,
    "tear": 0.60,
}


def _calc_scratch_profile(style: str, dur: float, rate: int) -> tuple[np.ndarray, np.ndarray, float, float, float]:
    """計算指定刷碟手法的速度曲線 v(t)、Crossfader 門閥包絡 A(t)、基準原點 p0、摩擦增益、Rumble增益。"""
    n = int(rate * dur)
    t = np.linspace(0, dur, n)
    v = np.zeros(n)
    fader = np.ones(n)
    p0 = 0.8
    fric_gain = 0.40
    rumble_gain = 0.25

    if style == "spinback":
        # 急速倒盤回甩 (Spinback / Backspin)
        v = -8.5 * np.exp(-3.8 * t) * (np.maximum(0.0, 1.0 - t / dur) ** 0.5)
        p0 = 1.8  # 往回倒轉需更多前置音訊
        fader = np.maximum(0.0, 1.0 - (t / dur) ** 1.8)
        fric_gain = 0.55
        rumble_gain = 0.20

    elif style == "vinyl_brake":
        # 轉盤斷電慢速煞車 (Turntable Stop / Pitch Drop)
        v = (np.maximum(0.0, 1.0 - t / dur)) ** 1.6
        p0 = 0.1
        fader = np.maximum(0.0, 1.0 - (t / dur) ** 1.3)
        fric_gain = 0.25
        rumble_gain = 0.45

    elif style == "chirp":
        # 清脆鳥鳴短切刷 (Chirp Scratch)——切點比例取自原始 0.52s 手感，等比例縮放
        # 到任意 dur，讓 BPM 拼接出的短/長節奏段落都維持同樣的顆粒感。
        cuts = [f * dur for f in (0.0, 0.2308, 0.4808, 0.7308, 1.0)]
        for i in range(4):
            t_s, t_e = cuts[i], cuts[i + 1]
            seg = (t >= t_s) & (t < t_e)
            if not np.any(seg):
                continue
            tn = (t[seg] - t_s) / (t_e - t_s)
            spd = [4.2, -4.2, 4.5, -4.0][i]
            v[seg] = spd * _sinpow(np.sin(np.pi * tn), 1.3)
        p0 = 0.6
        # Crossfader 閘門：換向低速處關閉，高峰處全開
        spd_abs = np.abs(v)
        fader = np.clip((spd_abs - 0.8) / 1.5, 0.0, 1.0) ** 1.5
        fric_gain = 0.45
        rumble_gain = 0.20

    elif style == "transform":
        # 機關槍 16 分音符節奏切片刷 (Transform Scratch)——切點比例取自原始 0.65s
        cuts = [f * dur for f in (0.0, 0.3846, 0.6923, 1.0)]
        v1 = (t < cuts[1])
        v[v1] = 1.8 * np.sin(np.pi * t[v1] / cuts[1])
        v2 = (t >= cuts[1]) & (t < cuts[2])
        v[v2] = -2.2 * np.sin(np.pi * (t[v2] - cuts[1]) / (cuts[2] - cuts[1]))
        v3 = (t >= cuts[2])
        v[v3] = 3.0 * np.sin(np.pi * (t[v3] - cuts[2]) / (dur - cuts[2]))
        p0 = 0.8
        # 14Hz 方波切音
        gate = np.sin(2 * np.pi * 14.0 * t)
        fader = np.where(gate > 0.0, 1.0, 0.08)
        fric_gain = 0.35
        rumble_gain = 0.25

    elif style == "tear":
        # 雙速撕裂刷 (Tear Scratch)——切點比例取自原始 0.60s
        cuts = [f * dur for f in (0.0, 0.1667, 0.4333, 0.6333, 1.0)]
        s1 = (t >= cuts[0]) & (t < cuts[1])
        v[s1] = 1.8 * np.sin(np.pi * (t[s1] - cuts[0]) / (cuts[1] - cuts[0]))
        s2 = (t >= cuts[1]) & (t < cuts[2])
        v[s2] = 4.2 * _sinpow(np.sin(np.pi * (t[s2] - cuts[1]) / (cuts[2] - cuts[1])), 1.3)
        s3 = (t >= cuts[2]) & (t < cuts[3])
        v[s3] = -1.6 * np.sin(np.pi * (t[s3] - cuts[2]) / (cuts[3] - cuts[2]))
        s4 = (t >= cuts[3]) & (t <= cuts[4])
        v[s4] = -4.0 * _sinpow(np.sin(np.pi * (t[s4] - cuts[3]) / (cuts[4] - cuts[3])), 1.3)
        p0 = 0.7
        fric_gain = 0.45
        rumble_gain = 0.25

    else:  # "wicka" 預設——切點比例取自原始 0.65s
        cuts = [f * dur for f in (0.0, 0.2, 0.4, 0.5846, 1.0)]
        s1 = (t >= cuts[0]) & (t < cuts[1])
        v[s1] = -2.6 * _sinpow(np.sin(np.pi * (t[s1] - cuts[0]) / (cuts[1] - cuts[0])), 1.3)
        s2 = (t >= cuts[1]) & (t < cuts[2])
        v[s2] = 3.2 * _sinpow(np.sin(np.pi * (t[s2] - cuts[1]) / (cuts[2] - cuts[1])), 1.3)
        s3 = (t >= cuts[2]) & (t < cuts[3])
        v[s3] = -3.5 * _sinpow(np.sin(np.pi * (t[s3] - cuts[2]) / (cuts[3] - cuts[2])), 1.4)
        s4 = (t >= cuts[3]) & (t <= cuts[4])
        v[s4] = 4.0 * np.sin(np.pi * (t[s4] - cuts[3]) / (cuts[4] - cuts[3]) * 0.75) * np.exp(-3.2 * (t[s4] - cuts[3]) / (cuts[4] - cuts[3]))
        p0 = 0.8
        fric_gain = 0.42
        rumble_gain = 0.28

    return v, fader, p0, fric_gain, rumble_gain


def _gen_scratch_segment(raw_pcm: np.ndarray, rate: int, style: str, dur: float) -> np.ndarray:
    """單一刷碟手勢（風格＋時值）→ 一段刷碟音訊。gen_scratch_from_pcm 依 BPM 拆出
    的半分/三連/四分節奏，每一段各自呼叫這裡再串接，聽起來像連續數個刷碟動作，
    而非同一個固定循環反覆播放。"""
    n = int(rate * dur)
    v, fader, p0, fric_gain, rumble_gain = _calc_scratch_profile(style, dur, rate)

    # 隨手速積分重採樣
    pos = p0 + np.cumsum(v) / rate
    pos_idx = np.clip(pos * rate, 0, len(raw_pcm) - 2)
    idx_f = pos_idx.astype(int)
    idx_frac = pos_idx - idx_f
    scratched_tone = (1.0 - idx_frac) * raw_pcm[idx_f] + idx_frac * raw_pcm[idx_f + 1]

    # 手速動態包絡 (速度越快聲音越響亮，換向停滯點無聲)
    speed = np.abs(v)
    vol_env = np.clip(speed / 1.6, 0.0, 1.0) ** 0.8 * fader

    # 唱針微觀摩擦層 (Needle Friction & Texture)
    rng = np.random.default_rng(2026)
    noise = rng.normal(0, 1, n)
    sos_fric = butter(2, [800, min(5200, int(rate * 0.45))], btype="bandpass", fs=rate, output="sos")
    friction = sosfilt(sos_fric, noise) * fric_gain * (speed ** 1.15)

    # 黑膠唱盤箱體低頻 (Turntable Platter Rumble)
    sos_rumble = butter(2, [45, 110], btype="bandpass", fs=rate, output="sos")
    rumble = sosfilt(sos_rumble, rng.normal(0, 1, n)) * rumble_gain * (speed ** 0.5)

    # 黑膠唱針微小碎音 (Needle crackle / micro-pops)
    crackle = np.zeros(n)
    pop_indices = rng.choice(n, size=int(n * 0.008), replace=False)
    crackle[pop_indices] = rng.uniform(0.25, 0.7, size=len(pop_indices)) * speed[pop_indices]

    # 混音組合
    mix = (scratched_tone * 0.75 + friction * 0.38 + rumble * 0.25 + crackle * 0.15) * vol_env

    # 溫暖度濾波（抑制過度刺耳的高頻，留下扎實的中頻刷碟質感）
    sos_body = butter(2, min(6500, int(rate * 0.45)), btype="lowpass", fs=rate, output="sos")
    mix = sosfilt(sos_body, mix)

    # 軟飽和 (Analog Warmth)
    out = np.tanh(mix * 1.55)

    return _fade(out.astype(np.float32), attack=0.008, release=0.04)


def _pick_bpm_scratch_durations(bpm: float) -> list[float]:
    """把下一首的 BPM 拆成半分/三連/四分等固定音符時值，湊出 1~2 秒的刷碟節奏。

    最小單位是一個「bar」＝3 個音符，音符值各自從 note_pool 隨機挑（快慢交錯），
    bar 湊好後緊接著原樣複製一次形成「一組」（AABB 式的兩段重複），聽起來像真人
    刷了一個手法再重複一次，而不是每段都各刷各的、毫無律動可循。組數視 target
    隨機湊到 1~2 秒，極端 BPM（音符值 clamp 到頂/底）下最多堆到 4 組（12 段）
    避免無限迴圈。單一音符時值 clamp 在 [0.12, 0.9]s，避免極端 BPM 讓某一段獨佔
    整個效果或短到破音。"""
    beat = 60.0 / bpm
    note_pool = (beat * 2.0, beat * 1.0, beat * 2.0 / 3.0)  # 半分 / 四分 / 三連音兩份
    target = random.uniform(1.0, 2.0)
    durations: list[float] = []
    total = 0.0
    while total < target and len(durations) < 12:
        bar = [min(max(random.choice(note_pool), 0.12), 0.9) for _ in range(3)]
        durations.extend(bar)
        durations.extend(bar)
        total += sum(bar) * 2
    return durations or [min(max(target, 0.12), 0.9)]


def gen_scratch_from_pcm(raw_pcm: np.ndarray, rate: int = RATE, style: str | None = None,
                          bpm: float | None = None) -> np.ndarray:
    """使用傳入的音樂 PCM 進行真實 DJ 黑膠轉盤手刷調變。

    raw_pcm: float32 PCM（單聲道或雙聲道均可）
    rate: 採樣率（預設 44100，或 48000）
    style: 手法風格名稱（"wicka", "spinback", "vinyl_brake", "chirp", "transform", "tear"）。
           若為 None 則隨機從 SCRATCH_STYLES 挑選一種。bpm 給定時每段各自隨機挑，
           style 參數在該情境下不生效。
    bpm: 下一首歌的 BPM。給定時依半分/三連/四分節奏拆成多段（總長 1~2s）串接，
         段落數與時值隨機、不是固定節拍；為 None 時退回單一手法的舊行為
         （長度取自 _STYLE_DURATIONS，約 0.5~0.75s）。
    """
    if raw_pcm.ndim > 1:
        raw_pcm = np.mean(raw_pcm, axis=-1)

    raw_pcm = raw_pcm.astype(np.float32)

    # 摩擦/rumble/碎音幾層是照「內建 formant 素材」的音量（tanh 飽和後 RMS≈0.3）調的
    # 增益；真實歌曲 PCM（loudnorm 後 RMS 常只有 ~0.1-0.15）直接餵進來，噪點層蓋過
    # 刷碟音本身，聽起來變成一坨跟歌完全無關的電子噪音。這裡固定歸一化到統一峰值，
    # 讓 scratched_tone 音量不再看輸入原始響度臉色。
    peak = float(np.max(np.abs(raw_pcm))) if raw_pcm.size else 0.0
    if peak > 1e-6:
        raw_pcm = raw_pcm * (0.9 / peak)

    # 若輸入長度少於 3 秒，以循環/鏡像補足
    min_samples = int(rate * 3.0)
    if len(raw_pcm) < min_samples:
        repeats = int(np.ceil(min_samples / max(1, len(raw_pcm))))
        raw_pcm = np.tile(raw_pcm, repeats)[:min_samples]

    if bpm and bpm > 0:
        durations = _pick_bpm_scratch_durations(float(bpm))
        segments = [
            _gen_scratch_segment(raw_pcm, rate, random.choice(SCRATCH_STYLES), dur)
            for dur in durations
        ]
        return np.concatenate(segments) if len(segments) > 1 else segments[0]

    if style is None or style not in SCRATCH_STYLES:
        style = random.choice(SCRATCH_STYLES)
    dur = _STYLE_DURATIONS.get(style, 0.65)
    return _gen_scratch_segment(raw_pcm, rate, style, dur)


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

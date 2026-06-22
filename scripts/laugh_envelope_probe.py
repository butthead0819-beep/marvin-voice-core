"""驗證 A：對 records/laugh_samples/ 的 WAV 算節律特徵，看「笑」vs「講話」分不分得開。

弱標籤來自檔名（STT 抓到清楚哈哈→laughN、長句→speech）。
若兩群在 (peaks_per_sec, regularity) 上明顯分開 → A 可行、門檻可定；
若混在一起 → A 在這音訊上不可靠。

用法：venv_simon/bin/python scripts/laugh_envelope_probe.py
"""
import glob
import os
import sys
import wave

sys.path.insert(0, ".")
from laugh_acoustics import rms_envelope, rhythm_features, looks_like_laugh

SAMPLE_DIR = "records/laugh_samples"
FRAME_MS = 20


def _mono(path):
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        return None, sr
    import array
    a = array.array("h")
    a.frombytes(raw)
    if ch == 2:
        mono = [(a[i] + a[i + 1]) // 2 for i in range(0, len(a) - 1, 2)]
    else:
        mono = list(a)
    return mono, sr


def _summary(name, rows):
    if not rows:
        print(f"  {name}: （無樣本）")
        return
    pps = sorted(r["peaks_per_sec"] for r in rows)
    reg = sorted(r["regularity"] for r in rows)
    hit = sum(1 for r in rows if r["_laughish"])
    med = lambda xs: xs[len(xs) // 2]
    print(f"  {name}: n={len(rows)}  peaks/sec 中位={med(pps):.1f} [{pps[0]:.1f}–{pps[-1]:.1f}]"
          f"  regularity 中位={med(reg):.2f}  判定為笑={hit}/{len(rows)}")


def main():
    files = sorted(glob.glob(os.path.join(SAMPLE_DIR, "*.wav")))
    if not files:
        print(f"沒有樣本。先設 LAUGH_SAMPLE_CAPTURE=1 重啟 bot、跑一場有笑的對話，再執行本腳本。")
        return
    laughs, speeches = [], []
    for p in files:
        base = os.path.basename(p)
        mono, sr = _mono(p)
        if not mono:
            continue
        env = rms_envelope(mono, frame_len=int(sr * FRAME_MS / 1000))
        f = rhythm_features(env, frame_rate_hz=1000.0 / FRAME_MS)
        f["_laughish"] = looks_like_laugh(f)
        f["_file"] = base
        (laughs if base.startswith("laugh") else speeches).append(f)

    print(f"樣本 {len(files)} 個（laugh 錨 {len(laughs)} / speech 錨 {len(speeches)}）\n")
    print("== 分群統計（看兩群分不分得開）==")
    _summary("笑 (STT 清楚哈哈)", laughs)
    _summary("講話 (STT 長句)", speeches)
    # 分離度：speech 被誤判為笑的比例 = 假陽性風險
    if speeches:
        fp = sum(1 for r in speeches if r["_laughish"]) / len(speeches)
        print(f"\n假陽性（講話被判成笑）: {fp*100:.0f}%　→ 越低越好")
    if laughs:
        tp = sum(1 for r in laughs if r["_laughish"]) / len(laughs)
        print(f"真陽性（笑被判成笑）: {tp*100:.0f}%　→ 越高越好")


if __name__ == "__main__":
    main()

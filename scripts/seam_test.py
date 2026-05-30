"""Seam test — 雙緩衝熱切換 hack 的接縫品質離線評估。

背景：「播歌中途插 TTS」的 hack 是「背景開第二條同首歌 stream（-ss 到切換點）、
把 TTS 用 sidechain ducking 混進去、到點硬切第一條 stream」。混音部分免費（複用
DJ injection 的 FFmpeg sidechain filter），風險全在**切換接縫**：loudnorm 暫態、
相位 click、（live 才有的）timing jitter。

這個 script 把純音訊接縫渲染成 WAV 讓人耳判斷——隔離掉 Discord timing，只測
「硬切兩條獨立解碼的 stream」聽起來多糟、緩解手段有沒有用。

產出 3 個 25 秒 WAV（接縫 / TTS 起點都在第 10 秒，方便 audition）：
  seam_naive.wav     — part1(loudnorm) 硬切 part2(loudnorm + TTS sidechain)
                       production-faithful，暴露 loudnorm 暫態 + 相位 click（最差）
  seam_mitigated.wav — part1/part2 都用固定 volume（無 loudnorm，level 天生匹配）
                       + part2 開頭 afade=in 150ms 遮相位 click（緩解版）
  seam_reference.wav — 單一連續解碼，TTS 用 adelay 注入切換點。理想無縫對照
                       （= 若能改 running filter graph 的完美結果，但實際做不到）

聽法：focus 第 10 秒。naive 有沒有音量跳/爆音？mitigated 有沒有改善到可接受？
跟 reference 差多少？→ 決定這個 hack 值不值得做整套 live timing 工程。

用法：
  python scripts/seam_test.py                    # 用預設歌 + ack
  python scripts/seam_test.py --music X.mp3 --tts Y.mp3 --cut 95
"""
from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_MUSIC = ROOT / "assets/songs/《天亮，總是心碎》.mp3"
DEFAULT_TTS = ROOT / "assets/acks/ack_1.mp3"
OUT_DIR = ROOT / "records/seam_test"

# Discord 播放格式
AR, AC = 48000, 2
SIDECHAIN = "sidechaincompress=threshold=0.02:ratio=8:attack=5:release=600"


def _run(cmd: str) -> None:
    proc = subprocess.run(shlex.split(cmd), capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-1500:])
        raise SystemExit(f"ffmpeg failed: {cmd[:80]}...")


def _fmt(p: Path) -> str:
    return shlex.quote(str(p))


def render(music: Path, tts: Path, cut: float, pre: float, window: float, vol: float):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    seam_at = pre  # 接縫/TTS 在輸出檔的第幾秒
    tmp = OUT_DIR / "_tmp"
    tmp.mkdir(exist_ok=True)

    # ── part1：切換前 pre 秒 ──────────────────────────────────────────────
    # naive 用 loudnorm（production-faithful）；mitigated 用固定 volume
    p1_naive = tmp / "p1_naive.wav"
    p1_mit = tmp / "p1_mit.wav"
    _run(f"ffmpeg -y -ss {cut - pre} -i {_fmt(music)} -t {pre} "
         f"-af loudnorm=I=-14:TP=-1.5:LRA=11 -ar {AR} -ac {AC} {_fmt(p1_naive)}")
    _run(f"ffmpeg -y -ss {cut - pre} -i {_fmt(music)} -t {pre} "
         f"-af volume={vol:.3f} -ar {AR} -ac {AC} {_fmt(p1_mit)}")

    # ── part2：切換後 window 秒，TTS sidechain 混入 ───────────────────────
    # input 0 = TTS, input 1 = music seeked to cut（後置 -ss 求 sample 準確）
    p2_naive = tmp / "p2_naive.wav"
    p2_mit = tmp / "p2_mit.wav"
    fc_naive = (
        f"[0:a]asplit=2[sc][mix];[sc]apad=whole_dur=9999[pad];"
        f"[1:a]loudnorm=I=-14:TP=-1.5:LRA=11[music];"
        f"[music][pad]{SIDECHAIN}[ducked];"
        f"[ducked][mix]amix=inputs=2:duration=first:normalize=0[out]"
    )
    fc_mit = (
        f"[0:a]asplit=2[sc][mix];[sc]apad=whole_dur=9999[pad];"
        f"[1:a]volume={vol:.3f}[music];"
        f"[music][pad]{SIDECHAIN}[ducked];"
        f"[ducked][mix]amix=inputs=2:duration=first:normalize=0,afade=in:d=0.15[out]"
    )
    _run(f"ffmpeg -y -i {_fmt(tts)} -ss {cut} -i {_fmt(music)} "
         f"-filter_complex \"{fc_naive}\" -map [out] -t {window} -ar {AR} -ac {AC} {_fmt(p2_naive)}")
    _run(f"ffmpeg -y -i {_fmt(tts)} -ss {cut} -i {_fmt(music)} "
         f"-filter_complex \"{fc_mit}\" -map [out] -t {window} -ar {AR} -ac {AC} {_fmt(p2_mit)}")

    # ── 硬切接合（concat = sample-accurate butt-join = 真正的硬切）─────────
    for name, p1, p2 in (("seam_naive", p1_naive, p2_naive),
                         ("seam_mitigated", p1_mit, p2_mit)):
        out = OUT_DIR / f"{name}.wav"
        _run(f"ffmpeg -y -i {_fmt(p1)} -i {_fmt(p2)} "
             f"-filter_complex \"[0:a][1:a]concat=n=2:v=0:a=1[o]\" -map [o] "
             f"-ar {AR} -ac {AC} {_fmt(out)}")

    # ── reference：單一連續解碼，TTS adelay 到接縫點（理想無縫）───────────
    cut_ms = int(seam_at * 1000)
    fc_ref = (
        f"[1:a]adelay={cut_ms}:all=1[tts_d];"
        f"[tts_d]asplit=2[sc][mix];[sc]apad=whole_dur=9999[pad];"
        f"[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[music];"
        f"[music][pad]{SIDECHAIN}[ducked];"
        f"[ducked][mix]amix=inputs=2:duration=first:normalize=0[out]"
    )
    ref = OUT_DIR / "seam_reference.wav"
    _run(f"ffmpeg -y -ss {cut - pre} -i {_fmt(music)} -i {_fmt(tts)} "
         f"-filter_complex \"{fc_ref}\" -map [out] -t {pre + window} -ar {AR} -ac {AC} {_fmt(ref)}")

    # 清 tmp
    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()
    return seam_at


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--music", default=str(DEFAULT_MUSIC))
    ap.add_argument("--tts", default=str(DEFAULT_TTS))
    ap.add_argument("--cut", type=float, default=95.0, help="切換點秒數（預設 1:35）")
    ap.add_argument("--pre", type=float, default=10.0, help="切換前保留秒數")
    ap.add_argument("--window", type=float, default=15.0, help="切換後保留秒數")
    ap.add_argument("--vol", type=float, default=0.8, help="mitigated 固定音量（測試用 0.8 求 artifact 明顯）")
    args = ap.parse_args()

    music, tts = Path(args.music), Path(args.tts)
    if not music.exists():
        print(f"music not found: {music}", file=sys.stderr); return 1
    if not tts.exists():
        print(f"tts not found: {tts}", file=sys.stderr); return 1

    seam_at = render(music, tts, args.cut, args.pre, args.window, args.vol)

    print(f"✅ 完成 → {OUT_DIR}/")
    print(f"   music={music.name}  tts={tts.name}  cut={args.cut}s")
    print(f"\n🎧 聽法（接縫 + TTS 起點都在第 {seam_at:.0f} 秒）：")
    print("   seam_naive.wav     ← production filter 硬切，聽第 10s 有沒有音量跳/爆音")
    print("   seam_mitigated.wav ← 去 loudnorm + afade，聽接縫有沒有改善到可接受")
    print("   seam_reference.wav ← 理想無縫對照，跟前兩個比差多少")
    print(f"\n   open {OUT_DIR}/   # macOS 用 QuickTime 逐個聽")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

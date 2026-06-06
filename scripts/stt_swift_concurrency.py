"""測 Swift STT 的併發退化曲線：繞過 Semaphore(1)，直接同時開 N 個 macos_stt_bin，
量每個 process 的 wall-clock 怎麼隨併發數變化。回答「一台 Mac 能同時撐幾個語音房」。
純診斷，跑完即丟。"""
import os, time, glob, statistics, wave, concurrent.futures, subprocess

BASE_CTX = "Marvin,馬文,碼文,麻文,艾馬文,馬問,馬門,嗨馬文,Hi Marvin,Siri,阿公,瑪利歐"

def wav_dur(p):
    w = wave.open(p, "rb"); return w.getnframes() / w.getframerate()

# 用代表性的 ~4s WAV
wavs = sorted(glob.glob("tmp_stt_*.wav"), key=wav_dur)
ref = [w for w in wavs if 3 < wav_dur(w) < 6][0]
print(f"基準 WAV: {ref} ({wav_dur(ref):.1f}s)\n")

def one_run(_):
    env = dict(os.environ, STT_CONTEXT_STRINGS=BASE_CTX, STT_LOCALE="zh-TW")
    t = time.perf_counter()
    subprocess.run(["./macos_stt_bin", ref], capture_output=True, text=True, env=env, timeout=60)
    return (time.perf_counter() - t) * 1000

print(f"{'併發N':>5} | {'每process中位ms':>14} | {'最慢ms':>8} | {'batch總時ms':>11} | vs N=1")
print("-" * 70)
baseline = None
for N in [1, 2, 3, 4, 6, 8]:
    batch_t = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=N) as ex:
        durs = list(ex.map(one_run, range(N)))
    batch_ms = (time.perf_counter() - batch_t) * 1000
    med = statistics.median(durs)
    if baseline is None:
        baseline = med
    slow = med / baseline
    print(f"{N:5d} | {med:14.0f} | {max(durs):8.0f} | {batch_ms:11.0f} | {slow:.2f}x")
print("\n判讀：每process中位 ms 隨 N 平緩 → 能並行(Mac mini 可擴);"
      "\n      若 ~線性暴漲(N=8 ≈ 8x) → OS 層序列化,Mac mini 也救不了。")

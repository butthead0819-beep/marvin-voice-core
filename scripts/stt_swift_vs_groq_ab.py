"""一次性 A/B：同一個 WAV 同時打 macOS Swift STT 與 Groq whisper-large-v3-turbo，
計時 + 比對轉錄。純診斷，跑完即丟。"""
import os, time, subprocess, glob, statistics, wave
from pathlib import Path

# --- load GROQ_API_KEY from .env ---
for line in Path(".env").read_text().splitlines():
    if line.startswith("GROQ_API_KEY="):
        os.environ["GROQ_API_KEY"] = line.split("=", 1)[1].strip()

from groq import Groq
client = Groq()

BASE_CTX = "Marvin,馬文,碼文,麻文,艾馬文,馬問,馬門,嗨馬文,Hi Marvin,Siri,阿公,瑪利歐"

def wav_dur(p):
    w = wave.open(p, "rb"); return w.getnframes() / w.getframerate()

def run_swift(wav):
    env = dict(os.environ, STT_CONTEXT_STRINGS=BASE_CTX, STT_LOCALE="zh-TW")
    t = time.perf_counter()
    p = subprocess.run(["./macos_stt_bin", wav], capture_output=True, text=True, env=env, timeout=30)
    ms = (time.perf_counter() - t) * 1000
    text = ""
    for ln in p.stdout.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("__META__ ") or ln[0] in "🔍✅❌📚" or ln.startswith("DEBUG:"):
            continue
        text = ln
    return ms, text

def run_groq(wav):
    t = time.perf_counter()
    with open(wav, "rb") as f:
        r = client.audio.transcriptions.create(
            file=(os.path.basename(wav), f.read()),
            model="whisper-large-v3-turbo",
            language="zh",
        )
    ms = (time.perf_counter() - t) * 1000
    return ms, r.text.strip()

wavs = sorted(glob.glob("tmp_stt_*.wav"), key=wav_dur)
print(f"{'音長':>6} | {'Swift ms':>9} | {'Groq ms':>9} | 轉錄對比")
print("-" * 90)
sw_all, gq_all = [], []
for wav in wavs:
    d = wav_dur(wav)
    # 各跑 2 次取較快的（去掉冷啟動）
    sw = min(run_swift(wav)[0] for _ in range(2)); _, sw_txt = run_swift(wav)
    gq = min(run_groq(wav)[0] for _ in range(2)); _, gq_txt = run_groq(wav)
    sw_all.append(sw); gq_all.append(gq)
    print(f"{d:5.1f}s | {sw:9.0f} | {gq:9.0f} |")
    print(f"       | Swift: {sw_txt[:70]}")
    print(f"       | Groq : {gq_txt[:70]}")
    print("-" * 90)
print(f"\nSwift 中位 {statistics.median(sw_all):.0f}ms | Groq 中位 {statistics.median(gq_all):.0f}ms")

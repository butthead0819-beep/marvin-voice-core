"""雅婷 Yating 即時 ASR latency/品質 spike（一次性驗證，非正式整合）。

目的：在正式接 _speaker_lang=="nan" lane 前，先量「把一段完整 utterance WAV 當偽批次
丟雅婷 WebSocket」的端到端延遲，並看台語/台華夾雜的辨識品質。

協定（雅婷 dev doc）：
  1. POST https://asr.api.yating.tw/v1/token  header key:<API_KEY>  body {"pipeline": ...}
     → auth_token（60s、一次性）
  2. 連 wss://asr.api.yating.tw/ws/v1/?token=<auth_token>，收 {"status":"ok"}
  3. 送 16kHz/16-bit/mono PCM binary chunks（~2000 bytes），收 pipe.asr_final:true 為最終

金鑰只從環境變數讀，不寫進碼：  export YATING_API_KEY=...
用法：  python scripts/yating_spike.py <wav...> [--pipeline asr-zh-tw-std]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time

import requests
import websockets

TOKEN_URL = "https://asr.api.yating.tw/v1/token"
WS_URL = "wss://asr.api.yating.tw/ws/v1/?token={token}"
CHUNK_BYTES = 2000  # doc 建議 ~1/16s


def _wav_to_pcm16k(path: str) -> bytes:
    """用 ffmpeg 把任意 WAV 轉成 16kHz mono 16-bit LE raw PCM。"""
    out = subprocess.run(
        ["ffmpeg", "-i", path, "-ar", "16000", "-ac", "1", "-f", "s16le", "-loglevel", "error", "-"],
        capture_output=True, check=True,
    )
    return out.stdout


def _get_token(api_key: str, pipeline: str) -> tuple[str, float]:
    t0 = time.monotonic()
    resp = requests.post(
        TOKEN_URL,
        headers={"key": api_key, "Content-Type": "application/json"},
        json={"pipeline": pipeline},
        timeout=10,
    )
    dt = time.monotonic() - t0
    resp.raise_for_status()
    data = resp.json()
    token = data.get("auth_token") or data.get("authToken") or data.get("token")
    if not token:
        raise RuntimeError(f"token 回應無 auth_token: {data}")
    return token, dt


async def _run_one(api_key: str, pipeline: str, wav_path: str) -> None:
    print(f"\n=== {os.path.basename(wav_path)} ===")
    pcm = _wav_to_pcm16k(wav_path)
    audio_sec = len(pcm) / 2 / 16000
    print(f"音訊長度 {audio_sec:.2f}s（{len(pcm)} bytes PCM）")

    token, t_token = _get_token(api_key, pipeline)
    print(f"① token POST: {t_token*1000:.0f}ms")

    t_connect0 = time.monotonic()
    async with websockets.connect(WS_URL.format(token=token), max_size=None) as ws:
        t_connect = time.monotonic() - t_connect0
        # 等 {"status":"ok"}
        try:
            hello = await asyncio.wait_for(ws.recv(), timeout=10)
            print(f"② WS connect: {t_connect*1000:.0f}ms  hello={hello[:80]}")
        except asyncio.TimeoutError:
            print("② WS connect: 沒收到 hello（10s timeout）")

        finals: list[str] = []
        partials = 0

        async def _reader():
            nonlocal partials
            async for msg in ws:
                try:
                    obj = json.loads(msg)
                except Exception:
                    continue
                pipe = obj.get("pipe") or {}
                sent = pipe.get("asr_sentence", "")
                if pipe.get("asr_final"):
                    finals.append(sent)
                    print(f"   ✅ FINAL: 「{sent}」")
                elif sent:
                    partials += 1
                    print(f"   … partial: 「{sent}」")

        reader_task = asyncio.create_task(_reader())

        # ③ 盡快灌完所有 chunk（偽批次，量最小延遲）
        t_send0 = time.monotonic()
        for i in range(0, len(pcm), CHUNK_BYTES):
            await ws.send(pcm[i:i + CHUNK_BYTES])
        await ws.send(b"")  # 零長度 chunk 收尾
        t_send = time.monotonic() - t_send0
        print(f"③ 串流送出: {t_send*1000:.0f}ms")

        # ④ 等最終 transcript（從送完到拿到 final 的尾段處理時間最關鍵）
        t_wait0 = time.monotonic()
        while not finals and (time.monotonic() - t_wait0) < 20:
            await asyncio.sleep(0.05)
        t_tail = time.monotonic() - t_wait0
        reader_task.cancel()

    total = t_token + t_connect + t_send + t_tail
    print(f"④ 送完→final 尾段: {t_tail*1000:.0f}ms  (partials={partials})")
    print(f"—— 端到端 total ≈ {total*1000:.0f}ms（token {t_token*1000:.0f} + connect {t_connect*1000:.0f} "
          f"+ send {t_send*1000:.0f} + tail {t_tail*1000:.0f}）")
    if finals:
        print(f"—— 辨識結果：「{' '.join(finals)}」")
    else:
        print("—— ⚠️ 沒拿到 final transcript")


async def _main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("wavs", nargs="+")
    ap.add_argument("--pipeline", default="asr-zh-tw-std",
                    help="asr-zh-tw-std（國台語夾雜）/ asr-zh-en-std / asr-en-std")
    args = ap.parse_args()

    api_key = os.getenv("YATING_API_KEY")
    if not api_key:
        print("❌ 請先 export YATING_API_KEY=...")
        sys.exit(1)

    print(f"pipeline={args.pipeline}")
    for wav in args.wavs:
        try:
            await _run_one(api_key, args.pipeline, wav)
        except Exception as e:
            print(f"❌ {wav}: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(_main())

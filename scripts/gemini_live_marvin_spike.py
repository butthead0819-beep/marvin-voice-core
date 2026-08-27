"""Gemini Live API x Marvin 人格 —— 純本機 mic<->speaker 雙向語音 spike（不碰 Discord/骨幹）。

目的：接進 Marvin 正式骨幹前，先在本機驗證「Marvin 人格 + Gemini Live 原生語音對話」
的音色/延遲/人格還原度到底行不行。刻意繞開整條
Discord Audio Sink -> VAD -> STT -> Cleaner LLM -> IntentBus -> TTS 骨幹——
Live API 是端到端音訊對話，沒有可插入 IntentBus/judge race 的中間文字態，
接進去之前不值得動生產路徑一根寒毛（討論見 2026-08-27 對話）。

協定：google-genai SDK（requirements.txt 已釘 1.70.0）的
client.aio.live.connect()（bidiGenerateContent over WebSocket）。
麥克風 16kHz mono int16 PCM 送 session.send_realtime_input()；
session 回覆的音訊是 24kHz mono int16 PCM，收到就直接播放。

金鑰：只吃 GEMINI_PAID_API_KEY（獨立帳務，不要用免費 GOOGLE_API_KEY——
那把免費 key 的額度已經被 Tier-1 cleaner 吃到會 429，見 llm_pool.py:347 註解；
Live 音訊持續串流的 token 消耗率遠高於一次性文字請求，掛在同一把 key 上大概率
把其他免費流程一起拖垮）。

⚠️ 這是一次性驗證腳本，不是正式整合：
  - 下方呼叫（Blob/LiveConnectConfig/send_realtime_input/receive 等）已對照本機
    google-genai==1.70.0 的實際 type signature 核對過，欄位/參數名正確。
  - GEMINI_LIVE_MODEL 預設值可能已下架/改名（同 llm_pool.py 常踩的雷，這個
    model id 未經真實帳號連線驗證），先去 Google AI Studio 確認目前帳號可用的
    live/native-audio model 再跑，不對就用 env 覆蓋。

用法：
    export GEMINI_PAID_API_KEY=...
    venv_simon/bin/python scripts/gemini_live_marvin_spike.py
    對著麥克風跟馬文說話，Ctrl-C 結束。
"""
from __future__ import annotations

import asyncio
import os
import sys

INPUT_RATE = 16000
OUTPUT_RATE = 24000
CHUNK_MS = 20
CHUNK_SAMPLES = INPUT_RATE * CHUNK_MS // 1000

DEFAULT_MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-2.5-flash-native-audio-preview-09-2025")


def _build_marvin_system_instruction() -> str:
    """重用既有 qa_persona 人格層（見 marvin_prompts.py），只加一段語音場景補充。

    不呼叫 dna 敏感邏輯（dna=None）——這是單次口語 spike，不需要記憶/DNA 狀態機。
    """
    from marvin_prompts import PromptManager

    base = PromptManager().get_instruction("qa_persona")
    return (
        base
        + "\n\n【語音對話補充規則】這是即時雙向語音通話（不是文字訊息），回覆要用"
          "自然口語、正常講話的長度和節奏，不必條列、不必湊字數上限，講完一個念頭就停，"
          "像真人講電話一樣自然停頓與換氣。"
    )


async def _mic_to_session(session) -> None:
    import sounddevice as sd
    from google.genai import types

    loop = asyncio.get_running_loop()
    q: asyncio.Queue[bytes] = asyncio.Queue()

    def _callback(indata, frames, time_info, status):
        if status:
            print(f"[mic] {status}", file=sys.stderr)
        loop.call_soon_threadsafe(q.put_nowait, bytes(indata))

    with sd.RawInputStream(
        samplerate=INPUT_RATE, channels=1, dtype="int16",
        blocksize=CHUNK_SAMPLES, callback=_callback,
    ):
        print("開始收音，對著麥克風跟馬文說話（Ctrl-C 結束）...")
        while True:
            chunk = await q.get()
            await session.send_realtime_input(
                audio=types.Blob(data=chunk, mime_type=f"audio/pcm;rate={INPUT_RATE}")
            )


async def _session_to_speaker(session) -> None:
    import sounddevice as sd

    stream = sd.RawOutputStream(samplerate=OUTPUT_RATE, channels=1, dtype="int16")
    stream.start()
    try:
        while True:
            async for response in session.receive():
                if response.data:
                    stream.write(response.data)
                sc = response.server_content
                if sc and sc.input_transcription and sc.input_transcription.text:
                    print(f"[你說] {sc.input_transcription.text}")
                if sc and sc.output_transcription and sc.output_transcription.text:
                    print(f"[馬文] {sc.output_transcription.text}")
    finally:
        stream.stop()
        stream.close()


async def _main() -> None:
    api_key = os.getenv("GEMINI_PAID_API_KEY")
    if not api_key:
        print("请先 export GEMINI_PAID_API_KEY=...（不要用免費 GOOGLE_API_KEY，額度已經在跟 cleaner 搶）")
        sys.exit(1)

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=_build_marvin_system_instruction(),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )

    print(f"連線 Gemini Live（model={DEFAULT_MODEL}）...")
    async with client.aio.live.connect(model=DEFAULT_MODEL, config=config) as session:
        print("已連線。")
        await asyncio.gather(
            _mic_to_session(session),
            _session_to_speaker(session),
        )


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\n結束。")

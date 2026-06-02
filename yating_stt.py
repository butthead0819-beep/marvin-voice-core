"""雅婷 Yating 即時 ASR client — 台語/台華夾雜講者專用的雲端 STT lane。

為什麼雲端：本機 M1 8G 跑本地 Whisper 會 swap；雅婷是雲端 API，零本地模型、
零 RAM。pipeline asr-zh-tw-std 支援國台語夾雜，且輸出**正規化成繁體華語漢字**，
下游華語 pipeline 直接可用（不必處理台羅/台語漢字）。

協定（雅婷 dev doc）：
  1. POST {TOKEN_URL}  header key:<API_KEY>  body {"pipeline": ...} → auth_token（60s 一次性）
  2. 連 wss://.../ws/v1/?token=<auth_token>，收 {"status":"ok"}
  3. 送 16kHz/16-bit/mono PCM binary chunks，收 pipe.asr_final:true 為最終文字

pure helper（pcm16_from_float）可單測；transcribe 是 IO shell，lazy-import 網路套件，
缺套件/缺金鑰/逾時都回空字串，讓 caller 優雅降級回 Swift。
"""
from __future__ import annotations

import asyncio
import json

import numpy as np

TOKEN_URL = "https://asr.api.yating.tw/v1/token"
WS_URL = "wss://asr.api.yating.tw/ws/v1/?token={token}"
DEFAULT_PIPELINE = "asr-zh-tw-std"  # 國台語夾雜
_CHUNK_BYTES = 2000                  # doc 建議 ~1/16s


def pcm16_from_float(audio: np.ndarray) -> bytes:
    """16kHz mono float32（[-1,1]）→ 16-bit LE PCM bytes（雅婷要求格式）。"""
    if audio is None or len(audio) == 0:
        return b""
    clipped = np.clip(audio, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


async def _get_token(session, api_key: str, pipeline: str) -> str:
    async with session.post(
        TOKEN_URL,
        headers={"key": api_key, "Content-Type": "application/json"},
        json={"pipeline": pipeline},
        timeout=__import__("aiohttp").ClientTimeout(total=5),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
    token = data.get("auth_token") or data.get("authToken") or data.get("token")
    if not token:
        raise RuntimeError(f"token 回應無 auth_token: {data}")
    return token


async def transcribe(api_key: str, pcm: bytes, *,
                     pipeline: str = DEFAULT_PIPELINE, timeout: float = 8.0) -> str:
    """送一段完整 utterance PCM 給雅婷，回最終辨識文字（華語漢字）。

    任何失敗（缺套件 / 網路 / 逾時 / 無 final）都回 ""，caller 據此降級。
    """
    if not api_key or not pcm:
        return ""
    try:
        import aiohttp
        import websockets
    except ImportError:
        return ""

    async def _do() -> str:
        async with aiohttp.ClientSession() as session:
            token = await _get_token(session, api_key, pipeline)
        finals: list[str] = []
        async with websockets.connect(WS_URL.format(token=token), max_size=None) as ws:
            # 開場 status frame：非 ok（auth/quota 失敗）立即降級，不送音訊也不空等 final
            hello = await ws.recv()  # 期望 {"status":"ok"}
            try:
                if (json.loads(hello) or {}).get("status") != "ok":
                    return ""
            except (ValueError, TypeError):
                return ""
            for i in range(0, len(pcm), _CHUNK_BYTES):
                await ws.send(pcm[i:i + _CHUNK_BYTES])
            await ws.send(b"")  # 零長度 chunk 收尾
            while True:
                msg = await ws.recv()
                try:
                    pipe = (json.loads(msg) or {}).get("pipe") or {}
                except Exception:
                    continue
                if pipe.get("asr_final"):
                    finals.append(pipe.get("asr_sentence", ""))
                    break
        return " ".join(t for t in finals if t).strip()

    return await asyncio.wait_for(_do(), timeout=timeout)

"""Gemini 台語 STT shadow lane — 評估取代雅婷的候選引擎（2026-06-12）。

背景：台語講者（NAN_SPEAKER_IDS）走雅婷 asr-zh-tw-std，免費額度將盡，
全付費 ≈ NT$1,000+/月。候選解：Gemini Flash 收音訊直接轉華語漢字
（估 ~US$1.5/月，抽樣 25% 再省四倍）。品質未知 → shadow 模式收 3-5 天
對照數據（records/nan_stt_shadow.jsonl）再決策，同 judge race 套路。

設計約束：
- 主管線零影響：fire-and-forget task，任何失敗只寫 error 欄位
- env 閘控 NAN_STT_SHADOW（預設 off）+ 首次呼叫 log 狀態（J2 空轉教訓）
- key 優先用 GEMINI_PAID_API_KEY：免費 GOOGLE_API_KEY 的 quota 已被
  Tier-1/cleaner 吃光（2026-06-12 實測 429），shadow 數據不能餓死在 429
- 不走 LLM bus：bus 是 OpenAI-compat 文字池，音訊輸入走 genai 原生 client
  （同 STT cleaner 的既有先例）

分析：venv_simon/bin/python3 -c "import json;..." 或日後 scripts/analyze_nan_shadow.py
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import random
import time
import wave
from pathlib import Path
from typing import Awaitable, Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_OUT_PATH = Path("records/nan_stt_shadow.jsonl")
_TIMEOUT_S = 15.0

_PROMPT = (
    "這是一段台灣閩南語（台語，可能夾雜華語）的語音。"
    "請把說話內容轉寫成自然的台灣華語漢字（翻譯成華語口語書面），"
    "只輸出轉寫文字，不要任何解釋或標點以外的符號。"
    "若沒有可辨識的語音內容，輸出空字串。"
)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

_client_cache = None
_logged_once = False
# fire-and-forget task 必須保留參考，否則 CPython 可能在完成前 GC 掉（記錄無聲漏寫）
_bg_tasks: set = set()


# ── pure helpers ─────────────────────────────────────────────────────────────

def wav_from_float(audio: Optional[np.ndarray]) -> bytes:
    """16kHz mono float32（[-1,1]）→ in-memory WAV bytes（Gemini inline audio 用）。"""
    if audio is None or len(audio) == 0:
        return b""
    clipped = np.clip(audio, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)
    return buf.getvalue()


def shadow_enabled() -> bool:
    return os.environ.get("NAN_STT_SHADOW", "").strip().lower() in _TRUE_VALUES


def should_sample(rng: Callable[[], float] = random.random) -> bool:
    """抽樣閘：NAN_STT_SHADOW_RATE（預設 0.25）。控量 ~100 句/天，夠比對用。"""
    try:
        rate = float(os.environ.get("NAN_STT_SHADOW_RATE", "0.25"))
    except ValueError:
        rate = 0.25
    return rng() < rate


# ── IO shell ─────────────────────────────────────────────────────────────────

async def transcribe(client, audio: Optional[np.ndarray], *,
                     model: str | None = None, timeout: float = _TIMEOUT_S) -> str:
    """台語音訊 → 華語漢字。任何失敗回 ""（shadow 路徑，不影響主管線）。

    model 預設 gemini-2.5-flash（thinking 關掉省延遲與 token；換 2.0 系列
    需移除 thinking_config，否則 SDK 會拒）。
    """
    wav = wav_from_float(audio)
    if client is None or not wav:
        return ""
    model = model or os.environ.get("NAN_STT_GEMINI_MODEL", DEFAULT_MODEL)
    try:
        from google.genai import types
        resp = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=model,
                contents=[
                    _PROMPT,
                    types.Part.from_bytes(data=wav, mime_type="audio/wav"),
                ],
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                ),
            ),
            timeout=timeout,
        )
        return (resp.text or "").strip()
    except Exception as e:
        logger.warning(f"[NanShadow] Gemini transcribe 失敗: {type(e).__name__}: {e}")
        return ""


def _get_client():
    """lazy 單例 genai client。GEMINI_PAID_API_KEY 優先（free key quota 已被吃光）。"""
    global _client_cache
    if _client_cache is None:
        key = (os.environ.get("GEMINI_PAID_API_KEY")
               or os.environ.get("GOOGLE_API_KEY") or "").strip()
        if not key:
            return None
        from google import genai
        _client_cache = genai.Client(api_key=key)
    return _client_cache


async def run_shadow(audio, speaker: str, yating_text: str, *,
                     transcribe_fn: Callable[..., Awaitable[str]] | None = None,
                     out_path: Path = DEFAULT_OUT_PATH) -> None:
    """跑 Gemini 影子辨識並寫一筆對照記錄。絕不 raise（主管線零影響）。"""
    t0 = time.monotonic()
    gemini_text, error = "", None
    try:
        if transcribe_fn is None:
            client = _get_client()
            gemini_text = await transcribe(client, audio)
        else:
            gemini_text = await transcribe_fn(audio)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    latency_ms = int((time.monotonic() - t0) * 1000)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.time(),
                "speaker": speaker,
                "yating": yating_text,
                "gemini": gemini_text,
                "latency_ms": latency_ms,
                "error": error,
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"[NanShadow] 寫記錄失敗: {e}")


def maybe_shadow(audio, speaker: str, yating_text: str) -> None:
    """主管線唯一入口：env 閘 + 抽樣 + fire-and-forget。

    呼叫點必須在 running event loop 內（_process_stt_hybrid coroutine）。
    首次呼叫 log 啟用狀態 — env-gated shadow 要可驗證（J2 空轉 3 天教訓）。
    """
    global _logged_once
    if not _logged_once:
        _logged_once = True
        logger.info(
            f"[NanShadow] shadow={'ON' if shadow_enabled() else 'OFF'} "
            f"rate={os.environ.get('NAN_STT_SHADOW_RATE', '0.25')} "
            f"key={'paid' if os.environ.get('GEMINI_PAID_API_KEY') else 'free/none'}"
        )
    if not shadow_enabled() or not should_sample():
        return
    try:
        task = asyncio.create_task(run_shadow(audio, speaker, yating_text))
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
    except RuntimeError:
        pass  # 無 running loop（同步測試環境）→ 安靜跳過

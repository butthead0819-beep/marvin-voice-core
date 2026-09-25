"""Worker 尾段 STT cleaner（自 voice_controller 搬出，行為不變）+ Semantic ETD 結果重用。"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import pipeline_timing

logger = logging.getLogger(__name__)

ETD_REUSE_TTL_S = 30.0
_CACHE_ATTR = "_etd_clean_cache"


def _enabled() -> bool:
    return os.environ.get("MARVIN_ETD_CLEAN_REUSE", "1") != "0"


def remember_etd(ctrl, speaker, raw_text: str, res, now: float | None = None) -> None:
    """ETD 打完 cleaner 後呼叫：記住 (原句, 清洗後文字, 時間)，每個 speaker 只留最新一筆。"""
    if not speaker or not isinstance(res, dict):
        return
    text = res.get("text")
    if not isinstance(text, str) or not text.strip():
        return
    cache = ctrl.__dict__.setdefault(_CACHE_ATTR, {})
    cache[speaker] = (raw_text, text, time.time() if now is None else now)


def take_etd(ctrl, speaker, stripped: str, now: float | None = None,
             ttl: float = ETD_REUSE_TTL_S) -> str | None:
    """worker 用：取出並刪除該 speaker 的 ETD 結果；原句去喚醒詞後必須等於 stripped 才算命中。"""
    cache = ctrl.__dict__.get(_CACHE_ATTR)
    if not cache:
        return None
    entry = cache.pop(speaker, None)
    if entry is None:
        return None
    raw_text, cleaned, ts = entry
    if (time.time() if now is None else now) - ts > ttl:
        return None
    if ctrl._strip_wake_word(raw_text) != stripped:
        return None
    out = ctrl._strip_wake_word(cleaned).strip()
    return out or None


async def clean_for_worker(ctrl, stripped: str, speaker=None) -> str:
    """LLM 清洗 STT 雜訊，不做語音確認。短 timeout 封頂：cleaner 太慢就用 raw，不卡 worker
    （含 TimeoutError 由 except 接 → 降級 raw）。"""
    if _enabled() and speaker:
        reused = take_etd(ctrl, speaker, stripped)
        if reused is not None:
            logger.info(f"♻️ [ETD reuse] {speaker} '{stripped[:20]}' → '{reused[:20]}' 跳過第二次 cleaner")
            pipeline_timing.mark("cleaner_done")
            return reused

    cleaned = stripped
    if hasattr(ctrl.bot, "router") and hasattr(ctrl.bot.router, "clean_stt_text"):
        try:
            res = await asyncio.wait_for(
                ctrl.bot.router.clean_stt_text(stripped),
                timeout=ctrl._CONFIRM_CLEAN_TIMEOUT,
            )
            cleaned = res.get("text", stripped) if isinstance(res, dict) else stripped
        except Exception:
            pass
    pipeline_timing.mark("cleaner_done")
    return cleaned or stripped

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
_J3_CACHE_ATTR = "_j3_clean_cache"
J3_REUSE_TTL_S = 10.0


def _enabled() -> bool:
    return os.environ.get("MARVIN_ETD_CLEAN_REUSE", "1") != "0"


def remember_j3(ctrl, speaker: str, *keys: str, cleaned: str, now: float | None = None) -> None:
    """J3 Cleaner 結果快取：記錄多個 key（如 raw_text, stripped, cleaned）。"""
    if not speaker or not cleaned:
        return
    cache = ctrl.__dict__.setdefault(_J3_CACHE_ATTR, {})
    spk_cache = cache.setdefault(speaker, {})
    ts = time.time() if now is None else now
    for k in keys:
        if k and isinstance(k, str) and k.strip():
            spk_cache[k.strip()] = (cleaned, ts)


def take_j3(ctrl, speaker: str, text: str, *, now: float | None = None,
            ttl: float = J3_REUSE_TTL_S) -> str | None:
    """J3 cleaner 命中檢查：取出並刪除該 speaker 匹配 text 的清洗結果。"""
    if not _enabled() or not speaker or not text:
        return None
    cache = ctrl.__dict__.get(_J3_CACHE_ATTR)
    if not cache or speaker not in cache:
        return None
    spk_cache = cache[speaker]
    key = text.strip()
    if key not in spk_cache:
        return None
    cleaned, ts = spk_cache.pop(key)
    if not spk_cache:
        cache.pop(speaker, None)
    if (time.time() if now is None else now) - ts > ttl:
        return None
    return cleaned


def remember_etd(ctrl, speaker, raw_text: str, res, now: float | None = None) -> None:
    """ETD 打完 cleaner 後呼叫：記住 (原句, 清洗後文字, 時間)，每個 speaker 只留最新一筆。"""
    if not speaker or not isinstance(res, dict):
        return
    text = res.get("text")
    if not isinstance(text, str) or not text.strip():
        return
    cache = ctrl.__dict__.setdefault(_CACHE_ATTR, {})
    cache[speaker] = (raw_text, text, time.time() if now is None else now)
    remember_j3(ctrl, speaker, raw_text, text, cleaned=text, now=now)


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
            remember_j3(ctrl, speaker, stripped, reused, cleaned=reused)
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
    if speaker:
        remember_j3(ctrl, speaker, stripped, cleaned, cleaned=cleaned)
    return cleaned or stripped


async def run_real_cleaner_call(ctrl, c) -> str:
    """IntentBus real_cleaner_call 專用：先查 J3 reuse，未命中才打 clean_stt_text。"""
    reused = take_j3(ctrl, c.speaker, c.raw_text or "") or take_j3(ctrl, c.speaker, c.query or "")
    if reused:
        print(f"♻️ [J3 reuse] {c.speaker} 跳過 IntentBus 重複 cleaner: '{reused[:30]}'")
        return reused
    if not hasattr(ctrl.bot, "router") or not hasattr(ctrl.bot.router, "clean_stt_text"):
        return c.raw_text or c.query or ""
    recent_ctx = []
    if hasattr(ctrl.bot, "engine") and ctrl.bot.engine.conv_buffer:
        recent_ctx = ctrl.bot.engine.conv_buffer.get_last_n_utterances(5)
    res = await ctrl.bot.router.clean_stt_text(
        c.raw_text or c.query or "",
        context=recent_ctx,
        speaker=None,
        apply_gate=False,
    )
    return res.get("text", "")


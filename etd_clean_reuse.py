"""Worker 尾段 STT cleaner（自 voice_controller 搬出，行為不變）+ Semantic ETD 結果重用。"""
from __future__ import annotations

import asyncio

import pipeline_timing


async def clean_for_worker(ctrl, stripped: str) -> str:
    """LLM 清洗 STT 雜訊，不做語音確認。短 timeout 封頂：cleaner 太慢就用 raw，不卡 worker
    （含 TimeoutError 由 except 接 → 降級 raw）。"""
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

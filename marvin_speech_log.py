"""Marvin 每句實際播出的 TTS 完整台詞 + 開始時間，寫進獨立 log 給離線字幕工具用。

跟 `[TTS_TIMING]` print（cogs/voice_controller_playback.py，scripts/analyze_latency_breakdown.py
在解析它，只有前 30 字）與 STTHistory logger（daily review 會把整段送付費 Gemini）都無關——
這裡只記 Marvin 自己講出去的完整內容，不進 daily review 的付費呼叫。
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os

LOGGER_NAME = "MarvinSpeech"
LOG_PATH = "marvin_speech.log"

_origin = "discord"


def set_origin(origin: str) -> None:
    global _origin
    _origin = origin


def get_origin() -> str:
    return _origin


def configure_marvin_speech_logger(path: str = LOG_PATH) -> logging.Handler:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    target_abspath = os.path.abspath(path)
    for h in logger.handlers:
        if getattr(h, "baseFilename", None) == target_abspath:
            return h

    handler = logging.handlers.RotatingFileHandler(
        filename=path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return handler


def log_marvin_speech(
    text: str, *, start_ts: float, layer: int, voice: str | None, src: str, file: str | None = None
) -> None:
    try:
        clean = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").strip()
        if not clean:
            return
        payload = {
            "start": round(start_ts, 3),
            "layer": layer,
            "voice": voice,
            "src": src,
            "text": clean,
            "origin": _origin,
        }
        if file is not None:
            payload["file"] = file
        logging.getLogger(LOGGER_NAME).info(json.dumps(payload, ensure_ascii=False))
    except Exception:
        logging.getLogger(__name__).debug("log_marvin_speech 失敗", exc_info=True)


def log_song_start(title: str, *, start_ts: float, artist: str | None = None) -> None:
    """歌曲真正出聲那刻記一筆，給剪片工具放歌名卡用。"""
    try:
        clean = title.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").strip()
        if not clean:
            return
        payload = {
            "start": round(start_ts, 3),
            "src": "song",
            "text": clean,
            "origin": _origin,
        }
        if artist:
            payload["artist"] = artist
        logging.getLogger(LOGGER_NAME).info(json.dumps(payload, ensure_ascii=False))
    except Exception:
        logging.getLogger(__name__).debug("log_song_start 失敗", exc_info=True)

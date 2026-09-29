"""Shazam 音訊認歌（shazamio 非官方 API，失效自動退回呼叫端既有路徑）。

Why：`audiophile_fetcher.resolve_canon` 原本拿 YouTube 髒標題洗出來的歌名查 iTunes，
命中率約 40%。從串流切一段音訊送 Shazam 拿乾淨歌名再查 iTunes 換繁體+年份，
POC 命中率大幅提升（18/25、0 配錯）。

護欄：
  • shazamio 是非官方 API，隨時可能失效——任何例外/逾時/認不出一律回 None，
    絕不 raise 到呼叫端，不影響 DJ/播放。
  • 斷路器（ShazamBreaker）：連續失敗達門檻就暫停一小時，別在服務掛掉時
    每首歌都空等 timeout。
  • 抽音訊走 stdout pipe，不寫暫存檔。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from asyncio.subprocess import PIPE

logger = logging.getLogger(__name__)

SHAZAM_ENV = "MARVIN_SHAZAM"
CLIP_S = 12
DEFAULT_OFFSET_S = 60.0
IDENTIFY_TIMEOUT_S = 25.0
BREAKER_FAILS = 5
BREAKER_COOLDOWN_S = 3600.0

_ALBUM_TITLES = ("Album", "專輯", "专辑")

_import_warned = False


def enabled() -> bool:
    return os.getenv(SHAZAM_ENV, "1").lower() not in ("0", "false", "no", "")


def clip_offset(duration) -> float:
    """切片起點：太短的片段（<90s，如短版 MV）從一開始就可能是副歌，用 30% 位置；
    正常長度歌曲從第 60 秒切（跳過前奏）；沒有 duration 也回預設值。"""
    if not duration or duration >= 90:
        return DEFAULT_OFFSET_S
    return max(0.0, duration * 0.3)


def parse_track(resp: dict) -> dict | None:
    """Shazam recognize() 回應 → {"title", "artist", "album"}；認不出（無 track/曲名/
    歌手）回 None。album 從 sections[].metadata[] 掃「Album/專輯/专辑」那筆的 text。"""
    track = (resp or {}).get("track")
    if not track:
        return None
    title = track.get("title")
    artist = track.get("subtitle")
    if not title or not artist:
        return None

    album = None
    for section in track.get("sections") or []:
        for meta in section.get("metadata") or []:
            if meta.get("title") in _ALBUM_TITLES:
                album = meta.get("text")
                break
        if album:
            break

    return {"title": title.strip(), "artist": artist.strip(), "album": (album or "").strip() or None}


class ShazamBreaker:
    """連續失敗達 BREAKER_FAILS 次就斷路 BREAKER_COOLDOWN_S 秒——shazamio 是非官方
    API，掛掉時別讓每首歌都空等 timeout。任何一次成功會把失敗計數歸零。"""

    def __init__(self):
        self._fail_count = 0
        self._resume_at: float | None = None

    def allow(self, now: float) -> bool:
        if self._resume_at is not None and now < self._resume_at:
            return False
        return True

    def record(self, ok: bool, now: float) -> None:
        if ok:
            self._fail_count = 0
            return
        self._fail_count += 1
        if self._fail_count >= BREAKER_FAILS:
            self._resume_at = now + BREAKER_COOLDOWN_S
            self._fail_count = 0
            logger.warning(f"[Shazam] 連續失敗 {BREAKER_FAILS} 次，暫停 1 小時")


async def _extract_clip(stream_url: str, offset_s: float, clip_s: int = CLIP_S) -> bytes | None:
    """從串流切一段音訊（16kHz 單聲道 flac），走 stdout pipe，不寫暫存檔。
    flac 不用 wav：wav 寫進 pipe 沒法回填檔頭長度，shazamio 解碼端會狂噴「skipping junk」。
    被取消（identify 逾時）時 kill ffmpeg，不留孤兒程序在背景繼續下載。"""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error",
        "-ss", f"{offset_s:.1f}",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        "-i", stream_url,
        "-t", str(clip_s), "-vn", "-ac", "1", "-ar", "16000", "-f", "flac", "-",
        stdout=PIPE, stderr=PIPE,
    )
    try:
        stdout, _stderr = await proc.communicate()
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0 or not stdout or len(stdout) < 1000:
        return None
    return stdout


async def _recognize(data: bytes) -> dict:
    from shazamio import Shazam

    return await Shazam(language="zh-TW", endpoint_country="TW").recognize(data)


async def identify(
    stream_url: str,
    *,
    duration=None,
    breaker: ShazamBreaker,
    extract=None,
    recognize=None,
) -> dict | None:
    """stream_url → {"title", "artist", "album"} 或 None。零呼叫條件：關閉 / 沒
    stream_url / 斷路中。任何例外/逾時一律回 None、不 raise（DJ 串場不能因此掛掉）。"""
    global _import_warned

    if not enabled() or not stream_url:
        return None
    now = time.time()
    if not breaker.allow(now):
        return None

    try:
        async def _run():
            data = await (extract or _extract_clip)(stream_url, clip_offset(duration))
            if data is None:
                breaker.record(True, time.time())
                return None
            return await (recognize or _recognize)(data)

        resp = await asyncio.wait_for(_run(), IDENTIFY_TIMEOUT_S)
    except ImportError as e:
        if not _import_warned:
            logger.warning(f"[Shazam] shazamio 未安裝，停用音訊認歌: {e}")
            _import_warned = True
        return None
    except Exception as e:
        breaker.record(False, time.time())
        logger.info(f"[Shazam] 認歌例外: {e}")
        return None

    if resp is None:
        return None

    breaker.record(True, time.time())
    track = parse_track(resp)
    if track is None:
        logger.info("[Shazam] 認不出這段音訊")
        return None
    logger.info(f"[Shazam] 認到《{track['title']}》- {track['artist']}")
    return track

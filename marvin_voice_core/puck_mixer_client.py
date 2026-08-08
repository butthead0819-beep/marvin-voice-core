"""Puck mixer 控制平面 client：Mac → Pi mk2(device/puck_mixer.py) 的 /puck/* API。

跟 ESP32 車puck mk1 的 StreamSpeakerOutput（write(frame)/close() 被動吃 Mac 端混好的
PCM）是完全不同硬體架構——Pi mk2 算力夠，音樂自己抓流/解碼/混音，Mac 只送控制訊號
（歌曲 URL + 何時 crossfade）。見 project_car_puck_mk2_pi_zero2w_bt_mixer_validated 記憶。

DJ 口白（TTS）走另一條獨立的串流管線，不經這支 client——這裡純粹是音樂 A/B crossfade
的控制訊號，兩條管線互不相依。
"""
from __future__ import annotations

import logging

import aiohttp

logger = logging.getLogger(__name__)


class PuckMixerClient:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 10.0):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def _post(self, path: str, json_body: dict) -> bool:
        url = f"{self._base_url}{path}"
        params = {"t": self._token} if self._token else None
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(url, params=params, json=json_body) as resp:
                    if resp.status != 200:
                        logger.warning(f"[PuckMixer] {path} 失敗 status={resp.status}")
                    return resp.status == 200
        except Exception as e:
            logger.warning(f"[PuckMixer] {path} 連線失敗: {e}")
            return False

    async def play(self, url: str) -> bool:
        return await self._post("/puck/play", {"url": url})

    async def queue_next(self, url: str) -> bool:
        return await self._post("/puck/queue_next", {"url": url})

    async def crossfade(self, duration_s: float = 4.0) -> bool:
        return await self._post("/puck/crossfade", {"duration_s": duration_s})

    async def stop(self) -> bool:
        return await self._post("/puck/stop", {})

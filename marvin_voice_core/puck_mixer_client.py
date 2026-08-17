"""Puck mixer 控制平面 client：Mac → Pi mk2(device/puck_mixer.py) 的 /puck/* API。

跟 ESP32 車puck mk1 的 StreamSpeakerOutput（write(frame)/close() 被動吃 Mac 端混好的
PCM）是完全不同硬體架構——Pi mk2 算力夠，音樂自己抓流/解碼/混音，Mac 只送控制訊號
（歌曲 URL + 何時 crossfade）。見 project_car_puck_mk2_pi_zero2w_bt_mixer_validated 記憶。

play()/queue_next() 的 title 是選填：Pi 端會轉成 AVRCP MediaPlayer1 metadata
（見 device/avrcp_media_player.py），不影響播放本身，Pi 端沒裝 dbus 套件或
註冊失敗也只是車機螢幕沒曲名可顯示。

speak_text()：DJ 口白傳輸（2026-08-17 補上，見該記憶「DJ口白傳輸路線定案」）。
只送**文字**，不送音檔——Pi 自己呼叫 edge-tts 合成+疊播+duck 音樂（device/puck_mixer.py
的 speak()）。跟 esp32_edge_mix 的 PuckCommandQueueClient.speak(audio_path)（送 Mac
預渲染音檔路徑、ESP32 pull 播放）是完全不同的傳輸模型，兩種硬體的 client 型別不同，
呼叫端（cogs/music_cog.py::_maybe_play_dj_interjection）用 hasattr 分辨走哪條。
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

    async def play(self, url: str, title: str | None = None) -> bool:
        body = {"url": url}
        if title:
            body["title"] = title
        return await self._post("/puck/play", body)

    async def queue_next(self, url: str, title: str | None = None) -> bool:
        body = {"url": url}
        if title:
            body["title"] = title
        return await self._post("/puck/queue_next", body)

    async def crossfade(self, duration_s: float = 4.0) -> bool:
        return await self._post("/puck/crossfade", {"duration_s": duration_s})

    async def speak_text(self, text: str) -> bool:
        return await self._post("/puck/speak", {"text": text})

    async def stop(self) -> bool:
        return await self._post("/puck/stop", {})

"""Puck mixer 控制平面 client：Mac → Pi mk2(device/puck_mixer.py) 的 /puck/* API。

跟 ESP32 車puck mk1 的 StreamSpeakerOutput（write(frame)/close() 被動吃 Mac 端混好的
PCM）是完全不同硬體架構——Pi mk2 算力夠，音樂自己抓流/解碼/混音，Mac 只送控制訊號
（歌曲 URL + 何時 crossfade）。見 project_car_puck_mk2_pi_zero2w_bt_mixer_validated 記憶。

play()/queue_next() 的 title 是選填：Pi 端會轉成 AVRCP MediaPlayer1 metadata
（見 device/avrcp_media_player.py），不影響播放本身，Pi 端沒裝 dbus 套件或
註冊失敗也只是車機螢幕沒曲名可顯示。

seek：2026-08-19 補上——YouTube 熱力圖精華起點（cur_info['highlight_start_s']），
Discord 本地播放/DJ 口白排程本來就有算，但 Pi 端 resolve_stream_url() 一直沒
真的帶這個位移，Pi 永遠從第 0 秒開始播完整串流，實際內容比 Mac 排程假設的
長了 highlight_start_s 秒，長期會讓 Pi 端提早進入尾聲。現在跟 Mac 端
/puck_deck 既有的 seek 參數（原本是給斷線重連用的）共用，讓 Pi 也真的跳過
前奏，體驗跟 Discord 本地播放一致。

speak_text()：DJ 口白傳輸（2026-08-17 補上，見該記憶「DJ口白傳輸路線定案」）。
只送**文字**，不送音檔——Pi 自己呼叫 edge-tts 合成+疊播+duck 音樂（device/puck_mixer.py
的 speak()）。跟 esp32_edge_mix 的 PuckCommandQueueClient.speak(audio_path)（送 Mac
預渲染音檔路徑、ESP32 pull 播放）是完全不同的傳輸模型，兩種硬體的 client 型別不同，
呼叫端（cogs/music_cog.py::_maybe_play_dj_interjection）用 hasattr 分辨走哪條。
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp

logger = logging.getLogger(__name__)

# 2026-08-19 實機踩到：Mac→Pi 短暫 Tailscale 路由抖動時，queue_next/speak/play
# 三個呼叫全部連線層失敗（DNS/TCP 連不上，不是 Pi 回了非 200），一次就放棄，
# 白白錯過一整輪 crossfade——Pi 端 _write_with_reconnect（device/puck_mixer.py）
# 早就對這種瞬斷有重試，控制平面這幾個呼叫卻沒有。只對連線層例外重試；HTTP
# 回應本身（含非 200，例如 crossfade 時 deck_b 還沒 ready）是 Pi 端已經正常
# 回應、業務邏輯拒絕，重試沒有意義，不動它。
_RETRY_ATTEMPTS = 2
_RETRY_DELAY_S = 0.5


class PuckMixerClient:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 10.0):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def _post(self, path: str, json_body: dict) -> bool:
        url = f"{self._base_url}{path}"
        params = {"t": self._token} if self._token else None
        last_exc: Exception | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                async with aiohttp.ClientSession(timeout=self._timeout) as session:
                    async with session.post(url, params=params, json=json_body) as resp:
                        if resp.status != 200:
                            logger.warning(f"[PuckMixer] {path} 失敗 status={resp.status}")
                        return resp.status == 200
            except Exception as e:
                last_exc = e
                if attempt < _RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(_RETRY_DELAY_S)
        logger.warning(f"[PuckMixer] {path} 連線失敗: {last_exc}")
        return False

    async def play(self, url: str, title: str | None = None, seek: float | None = None) -> bool:
        body = {"url": url}
        if title:
            body["title"] = title
        if seek:
            body["seek"] = seek
        return await self._post("/puck/play", body)

    async def queue_next(self, url: str, title: str | None = None, seek: float | None = None) -> bool:
        body = {"url": url}
        if title:
            body["title"] = title
        if seek:
            body["seek"] = seek
        return await self._post("/puck/queue_next", body)

    async def crossfade(self, duration_s: float = 4.0) -> bool:
        return await self._post("/puck/crossfade", {"duration_s": duration_s})

    async def status(self) -> dict | None:
        """GET /puck/status——輪詢用，讓呼叫端知道 queue_next() 背景 resolve（yt-dlp+
        cookies 解 JS challenge，Pi 上可能要 20s+）真的完成、deck_b 已 ready，
        不用再賭一個固定 sleep 夠不夠久（見 _fire_puck_crossfade 呼叫點）。
        連線失敗/逾時回 None，呼叫端自行降級。"""
        url = f"{self._base_url}/puck/status"
        params = {"t": self._token} if self._token else None
        last_exc: Exception | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                async with aiohttp.ClientSession(timeout=self._timeout) as session:
                    async with session.get(url, params=params) as resp:
                        if resp.status != 200:
                            return None
                        return await resp.json()
            except Exception as e:
                last_exc = e
                if attempt < _RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(_RETRY_DELAY_S)
        logger.warning(f"[PuckMixer] /puck/status 連線失敗: {last_exc}")
        return None

    async def speak_text(self, text: str) -> bool:
        return await self._post("/puck/speak", {"text": text})

    async def stop(self) -> bool:
        return await self._post("/puck/stop", {})

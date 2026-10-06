"""單一 mixer 第 1 刀：Discord 進程 mixer 輸出 → /audio_stream 給車載 puck。

Discord 進程的 LocalMixingAudioSource 每 20ms 被 AudioPlayer 執行緒 read() 一次，這裡在
mixer 上掛一個 StreamSpeakerOutput 當旁聽者（set_tap），把同一份輸出原樣推給
GET /audio_stream（MP3 chunked）。車 puck 因此不用再靠 Pi 端 mixer，Discord 進程就是單一
混音來源。

env `MARVIN_DISCORD_STREAM_PORT` 未設＝不啟動、不碰 mixer，零行為改變。

注意：mixer 是 on_demand，閒置或 bot 不在語音頻道時 read() 不會被呼叫＝沒有幀，車 puck 的
ffmpeg 會卡住。SilenceFillQueue 在佇列空檔補靜音維持串流連續；刻意「不」去動 mixer 的
arm 邏輯（那是 2026-09-17 4021 重武裝風暴事故區）。
"""
from __future__ import annotations

import asyncio
import logging
import os

from aiohttp import web

from marvin_voice_core.audio_stream_batcher import iter_batched_encoded_frames
from marvin_voice_core.mp3_stream_encoder import Mp3StreamEncoder
from marvin_voice_core.stream_speaker_output import StreamSpeakerOutput

logger = logging.getLogger(__name__)

_FRAME_BYTES = 3840                    # 48k stereo s16 20ms
_SILENCE_FILL_FRAMES = 12
_SILENCE_FILL_TIMEOUT_S = 0.24
_MP3_KBPS = int(os.getenv("MARVIN_AUDIO_STREAM_MP3_KBPS", "128"))
_BATCH_BYTES = max(1, _MP3_KBPS * 1000 // 8 * int(os.getenv("MARVIN_AUDIO_STREAM_BATCH_MS", "100")) // 1000)

_CORS = {"Access-Control-Allow-Origin": "*",
         "Access-Control-Allow-Headers": "*",
         "Access-Control-Allow-Methods": "POST, OPTIONS"}


class SilenceFillQueue:
    """包住 asyncio.Queue：佇列空檔超過 timeout 就回一段靜音，讓串流不斷。

    為什麼需要：Discord mixer 是 on_demand，閒置或 bot 不在語音頻道時 read() 不被呼叫，
    就沒有任何幀進佇列；車 puck 的 ffmpeg 會因此卡住。這裡在沒有真實幀時補靜音維持連續。
    刻意不碰 mixer 的 arm 邏輯（見模組 docstring）。
    """

    def __init__(self, q, *, timeout_s=_SILENCE_FILL_TIMEOUT_S, fill_frames=_SILENCE_FILL_FRAMES):
        self._q = q
        self._timeout_s = timeout_s
        self._silence = b"\x00" * (_FRAME_BYTES * fill_frames)

    async def get(self) -> bytes | None:
        try:
            return await asyncio.wait_for(self._q.get(), self._timeout_s)
        except asyncio.TimeoutError:
            return self._silence


def build_app(stream_source, *, token: str | None) -> web.Application:
    @web.middleware
    async def _token_gate(request, handler):
        # 同 main_satellite._token_gate：token 為 None 不驗；OPTIONS 放行；
        # token 可走 X-Marvin-Token header 或 ?t=。
        if token and request.method != "OPTIONS":
            tok = request.headers.get("X-Marvin-Token") or request.query.get("t")
            if tok != token:
                return web.json_response({"error": "unauthorized"}, status=401, headers=_CORS)
        return await handler(request)

    async def handle_audio_stream(request):
        """GET /audio_stream — 同 main_satellite.handle_audio_stream；唯一差異是佇列外包
        SilenceFillQueue（mixer 閒置時補靜音）。"""
        if stream_source is None:
            return web.Response(status=404, headers=_CORS)
        resp = web.StreamResponse(status=200, headers={
            **_CORS, "Content-Type": "audio/mpeg",
            "X-Audio-Codec": "mp3",
            "X-Audio-Rate": str(stream_source.rate),
            "X-Audio-Channels": str(stream_source.channels),
            "X-Audio-Bits": str(stream_source.bits),
        })
        await resp.prepare(request)
        q = stream_source.subscribe()
        encoder = Mp3StreamEncoder(
            rate=stream_source.rate, channels=stream_source.channels,
            bitrate_kbps=_MP3_KBPS)
        try:
            async for chunk in iter_batched_encoded_frames(
                    SilenceFillQueue(q, timeout_s=_SILENCE_FILL_TIMEOUT_S,
                                     fill_frames=_SILENCE_FILL_FRAMES),
                    encoder, min_bytes=_BATCH_BYTES):
                await resp.write(chunk)
        except (ConnectionError, asyncio.CancelledError):
            # ConnectionError 涵蓋 BrokenPipeError/ConnectionResetError 與 aiohttp 包裝後的
            # ConnectionError("Connection lost")，puck 斷線時安靜結束（同 main_satellite）。
            pass
        finally:
            stream_source.unsubscribe(q)
        return resp

    app = web.Application(middlewares=[_token_gate])
    app.router.add_get("/audio_stream", handle_audio_stream)
    return app


class DiscordAudioStreamServer:
    def __init__(self, stream_source, *, port: int, token: str | None, host: str = "0.0.0.0"):
        self._stream_source = stream_source
        self._port = port
        self._token = token
        self._host = host
        self._runner: web.AppRunner | None = None

    async def start(self):
        app = build_app(self._stream_source, token=self._token)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        try:
            await site.start()
            logger.info(f"[DiscordAudioStream] Listening on {self._host}:{self._port} "
                        f"(token={'on' if self._token else 'off'})")
        except OSError as e:
            logger.warning(f"[DiscordAudioStream] Could not bind port {self._port}: {e} — /audio_stream unavailable")

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()


async def maybe_start(loop, vc) -> "DiscordAudioStreamServer | None":
    """env MARVIN_DISCORD_STREAM_PORT 未設或空字串 → None，不碰 mixer。"""
    raw = os.getenv("MARVIN_DISCORD_STREAM_PORT", "").strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError:
        logger.warning(f"[DiscordAudioStream] MARVIN_DISCORD_STREAM_PORT={raw!r} 不是數字，/audio_stream 不啟動")
        return None
    mixer = getattr(vc, "_mixer", None)
    if mixer is None:
        logger.warning("[DiscordAudioStream] voice controller 沒有 _mixer，/audio_stream 不啟動")
        return None
    stream_out = StreamSpeakerOutput(loop)
    mixer.set_tap(stream_out)
    token = os.getenv("MARVIN_TEXT_TOKEN", "").strip() or None
    server = DiscordAudioStreamServer(stream_out, port=port, token=token)
    await server.start()
    return server

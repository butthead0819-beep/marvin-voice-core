"""Wyoming→Marvin 橋——Pi 衛星麥（wyoming-satellite）接進 Marvin 現有 pipeline。

實體音箱 S2（唯一要自寫的一塊；其餘 Pi 側全用現成 wyoming-satellite）。架構：

    Pi: wyoming-satellite(server, tcp://0.0.0.0:10700) ←── 本橋(client, 在 Mac 腦上)
        ├─ 收: Detection(喚醒候選→duck hook) + AudioChunk(16k mono s16)
        │      → 升 48k stereo → 餵 LocalMicSink._process_chunk（複用自適應底噪+1.5s 切句）
        │      → on_speech_cut_callback（= engine.process_audio_slice，同 LocalMicSink 契約）
        └─ 送: AudioStart/AudioChunk/AudioStop（TTS/mixer 音訊 → 衛星 snd-command 播放）

協定＝JSONL+PCM over TCP（wyoming 1.10）。satellite 是 server、腦是 client 連過去，
連上先送 RunSatellite；喚醒在 Pi 本地（--wake-uri 接 wyoming-openwakeword）。

身分：user_id 參數（預設 "satellite"）傳給切句 callback，由 speaker_provider 映射到
既有講者身分（如 OWNER_SPEAKER=狗與露）＝記憶延續（見 project_identity_unification）。

測試：client_factory 注入假 client / 對 localhost 假 satellite server 跑真 TCP，零硬體。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

import numpy as np

from marvin_voice_core.local_mic_sink import LocalMicSink

logger = logging.getLogger(__name__)

_SEND_CHUNK_BYTES = 3840  # 20ms @ 48k stereo s16（送播放音訊的切塊大小）


def upsample_16k_mono_to_48k_stereo(pcm: bytes) -> bytes:
    """16k mono s16 → 48k stereo s16（線性內插 ×3 + L=R interleave）。

    衛星麥 16k mono；下游 pipeline 契約 48k stereo（同 LocalMicSink）。線性內插
    避免 np.repeat 的階梯高頻假象；STT 引擎內部會再重採，品質足夠。
    """
    mono = np.frombuffer(pcm, dtype=np.int16)
    if mono.size == 0:
        return b""
    x = np.arange(mono.size, dtype=np.float64)
    xi = np.arange(mono.size * 3, dtype=np.float64) / 3.0
    up = np.clip(np.interp(xi, x, mono.astype(np.float64)), -32768, 32767).astype(np.int16)
    return LocalMicSink._mono_to_stereo(up.tobytes())


class WyomingSatelliteBridge:
    """連 wyoming-satellite 的薄 client：音訊入 pipeline、Detection 出 hook、播放音訊回送。"""

    def __init__(
        self,
        on_speech_cut_callback: Callable[..., Awaitable[None]],
        *,
        host: str = "127.0.0.1",
        port: int = 10700,
        user_id: str = "satellite",
        on_detection: Callable[[str], None] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        client_factory: Callable[[], object] | None = None,
        rms_threshold: int = 500,
        silence_cut_s: float = 1.5,
    ) -> None:
        self._host = host
        self._port = port
        self._on_detection = on_detection
        self._client_factory = client_factory
        self._client = None
        self._stop = asyncio.Event()
        # 複用 LocalMicSink 當 VAD/切句引擎（自適應底噪+時間基準切句全繼承），
        # source 不用（我們直接餵 _process_chunk）、user_id 帶衛星身分。
        self._sink = LocalMicSink(
            on_speech_cut_callback, loop=loop, source=[],
            rms_threshold=rms_threshold, silence_cut_s=silence_cut_s,
            user_id=user_id,
        )

    @property
    def sink(self):
        """內部 VAD/切句引擎（LocalMicSink）。Sentinel 心跳監控（engine.get_active_sink）
        監的是這顆，故 start_satellite_listening 把它掛上 engine.sink——與本機模式同型。"""
        return self._sink

    # ── 對外：衛星喇叭播放（TTS / mixer 音訊） ─────────────────────────────────

    async def send_pcm(self, pcm: bytes, *, rate: int = 48000, channels: int = 2) -> None:
        """把一段 s16 PCM 送到衛星播放（AudioStart→Chunk…→AudioStop）。

        衛星 snd-command 需配同格式（aplay -r 48000 -c 2 -f S16_LE）。"""
        if self._client is None or not pcm:
            return
        from wyoming.audio import AudioChunk, AudioStart, AudioStop  # noqa: PLC0415

        await self._client.write_event(AudioStart(rate=rate, width=2, channels=channels).event())
        for i in range(0, len(pcm), _SEND_CHUNK_BYTES):
            await self._client.write_event(
                AudioChunk(rate=rate, width=2, channels=channels,
                           audio=pcm[i:i + _SEND_CHUNK_BYTES]).event())
        await self._client.write_event(AudioStop().event())

    # ── 主迴圈 ────────────────────────────────────────────────────────────────

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """連上衛星、送 RunSatellite、進事件迴圈直到 stop()/斷線。

        斷線回傳（不自動重連）——caller 外層自行 while-loop 重連（對齊優雅降級：
        衛星斷線不該炸掉腦，pipeline 其他 transport 照常）。"""
        from wyoming.audio import AudioChunk  # noqa: PLC0415
        from wyoming.satellite import RunSatellite, StreamingStarted, StreamingStopped  # noqa: PLC0415
        from wyoming.wake import Detection  # noqa: PLC0415

        if self._client_factory is not None:
            self._client = self._client_factory()
        else:
            from wyoming.client import AsyncTcpClient  # noqa: PLC0415
            self._client = AsyncTcpClient(self._host, self._port)

        await self._client.connect()
        await self._client.write_event(RunSatellite().event())
        logger.info("🛰️ [WyomingBridge] 已連上衛星 %s:%s（RunSatellite 已送）", self._host, self._port)

        try:
            while not self._stop.is_set():
                event = await self._client.read_event()
                if event is None:  # 斷線
                    logger.warning("🛰️ [WyomingBridge] 衛星斷線")
                    break
                if AudioChunk.is_type(event.type):
                    chunk = AudioChunk.from_event(event)
                    if chunk.rate != 16000 or chunk.channels != 1 or chunk.width != 2:
                        # 衛星 mic-command 配錯格式：明講，別靜默吃壞音訊
                        logger.warning(
                            "🛰️ [WyomingBridge] 非預期音訊格式 rate=%s ch=%s w=%s（要 16k/1ch/2B），丟棄",
                            chunk.rate, chunk.channels, chunk.width)
                        continue
                    self._sink._process_chunk(
                        upsample_16k_mono_to_48k_stereo(chunk.audio), time.time())
                elif Detection.is_type(event.type):
                    det = Detection.from_event(event)
                    logger.info("🛰️ [WyomingBridge] 衛星喚醒候選 name=%s", det.name)
                    if self._on_detection is not None:
                        self._on_detection(det.name or "")
                elif StreamingStarted.is_type(event.type):
                    logger.info("🛰️ [WyomingBridge] 衛星開始串流")
                elif StreamingStopped.is_type(event.type):
                    logger.info("🛰️ [WyomingBridge] 衛星停止串流")
        finally:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass

"""
tests/test_stream_presence.py

TDD：GET /audio_stream?presence=1 — 串流連線即在場（手機電台 app 取代 ESP32 心跳）。

背景：iOS 捷徑沒辦法每 30 秒送 /car 心跳，所以讓 /audio_stream 帶 presence=1 時，
連線期間每送出一塊音訊就續期一次 CarPresence（等同心跳）。斷線不主動 absent，
交給既有 TTL（90s）收尾——隧道、基地台切換、app 重連這類短暫斷線不會讓 bot 撤離。

驗：
(a) presence=1 → 連上就 present(speaker)，觸發上車開場（on_arrive）
(b) 連線期間持續續期（present 被呼叫不只一次）
(c) 斷線不觸發 on_depart、在場名單還在（交給 TTL）
(d) 沒帶 presence → 完全不碰 CarPresence（ESP32 / Pi 既有行為不變）
(e) speaker 不在白名單 → 400，不開串流、不觸發上車
(f) 車載模式未接（car_presence=None）→ 照常串流，不報錯
(g) 串流專用 token 也能帶 presence=1（手機電台 app 只拿得到這把）
"""
from __future__ import annotations

import asyncio
import math
import struct
from unittest.mock import MagicMock

import pytest

from car_presence import CarPresence

MAIN = "main-s3cret"
STREAM = "stream-only"


def _make_vc():
    vc = MagicMock()
    vc.bot.cogs.get.return_value = None
    return vc


def _sine_pcm_frame(*, rate: int = 48000, channels: int = 2, seconds: float = 0.05) -> bytes:
    n = int(rate * seconds)
    samples = []
    for i in range(n):
        v = int(3000 * math.sin(2 * math.pi * 440 * i / rate))
        samples.extend([v] * channels)
    return struct.pack("<%dh" % len(samples), *samples)


class _FakeStreamSource:
    def __init__(self, n_frames: int = 0):
        self.rate, self.channels, self.bits = 48000, 2, 16
        self._n = n_frames
        self.subscribed = 0

    def subscribe(self):
        self.subscribed += 1
        q: asyncio.Queue = asyncio.Queue()
        for _ in range(self._n):
            q.put_nowait(_sine_pcm_frame())
        q.put_nowait(None)
        return q

    def unsubscribe(self, q):
        pass


class _SpyPresence(CarPresence):
    """真的 CarPresence 狀態機，外加記錄 present() 被呼叫幾次。"""

    def __init__(self):
        self.arrived: list[str] = []
        self.departed = 0
        self.present_calls: list[str] = []

        async def _arrive(spk):
            self.arrived.append(spk)

        async def _depart():
            self.departed += 1

        super().__init__(on_arrive=_arrive, on_depart=_depart)

    async def present(self, speaker: str = "") -> None:
        self.present_calls.append(speaker)
        await super().present(speaker)


def _app(*, presence, src, device_speakers=None, stream_token=STREAM):
    from car_http_app import build_text_app
    return build_text_app(_make_vc(), token=MAIN, stream_token=stream_token,
                          default_speaker="狗與露", car_presence=presence,
                          stream_source=src, device_speakers=device_speakers)


async def _get(app, path):
    from aiohttp.test_utils import TestClient, TestServer
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(path)
        body = await resp.read()
        return resp.status, body


@pytest.mark.asyncio
async def test_presence_param_triggers_arrive_with_speaker():
    p = _SpyPresence()
    status, _ = await _get(
        _app(presence=p, src=_FakeStreamSource(), device_speakers={"showay": "showay"}),
        f"/audio_stream?t={MAIN}&presence=1&speaker=showay")
    assert status == 200
    assert p.arrived == ["showay"]


@pytest.mark.asyncio
async def test_presence_without_speaker_uses_default_speaker():
    p = _SpyPresence()
    status, _ = await _get(_app(presence=p, src=_FakeStreamSource()),
                           f"/audio_stream?t={MAIN}&presence=1")
    assert status == 200
    assert p.arrived == ["狗與露"]


@pytest.mark.asyncio
async def test_presence_refreshed_while_streaming():
    p = _SpyPresence()
    status, body = await _get(_app(presence=p, src=_FakeStreamSource(n_frames=20)),
                              f"/audio_stream?t={MAIN}&presence=1")
    assert status == 200 and len(body) > 0
    assert len(p.present_calls) >= 2, "連線期間要持續續期，不能只在連上時 present 一次"
    assert set(p.present_calls) == {"狗與露"}
    assert p.arrived == ["狗與露"], "續期不可重複觸發上車開場"


@pytest.mark.asyncio
async def test_disconnect_does_not_depart_leaves_it_to_ttl():
    p = _SpyPresence()
    await _get(_app(presence=p, src=_FakeStreamSource(n_frames=4)),
               f"/audio_stream?t={MAIN}&presence=1")
    assert p.departed == 0
    assert p.occupants == ["狗與露"]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", "&presence=0", "&presence="])
async def test_no_presence_param_leaves_car_presence_untouched(query):
    p = _SpyPresence()
    status, _ = await _get(_app(presence=p, src=_FakeStreamSource(n_frames=4)),
                           f"/audio_stream?t={MAIN}{query}")
    assert status == 200
    assert p.present_calls == []
    assert p.arrived == []


@pytest.mark.asyncio
async def test_unknown_speaker_rejected_before_streaming():
    p = _SpyPresence()
    src = _FakeStreamSource()
    status, _ = await _get(
        _app(presence=p, src=src, device_speakers={"showay": "showay"}),
        f"/audio_stream?t={MAIN}&presence=1&speaker=stranger")
    assert status == 400
    assert p.arrived == [] and p.present_calls == []
    assert src.subscribed == 0


@pytest.mark.asyncio
async def test_presence_param_ignored_when_car_mode_off():
    status, body = await _get(_app(presence=None, src=_FakeStreamSource(n_frames=4)),
                              f"/audio_stream?t={MAIN}&presence=1")
    assert status == 200 and len(body) > 0


@pytest.mark.asyncio
async def test_stream_token_can_carry_presence():
    p = _SpyPresence()
    status, _ = await _get(_app(presence=p, src=_FakeStreamSource()),
                           f"/audio_stream?t={STREAM}&presence=1")
    assert status == 200
    assert p.arrived == ["狗與露"]

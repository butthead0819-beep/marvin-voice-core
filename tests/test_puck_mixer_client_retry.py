"""TDD：marvin_voice_core/puck_mixer_client.py::PuckMixerClient 連線層重試。

背景：2026-08-19 實機踩到 Mac→Pi 短暫 Tailscale 路由抖動時，queue_next/
speak/play/status 這幾個呼叫全部連線層失敗（DNS/TCP 連不上）一次就放棄，
白白錯過一整輪 crossfade。改成對連線層例外重試一次；HTTP 回應本身（含非
200，例如 crossfade 時 deck_b 還沒 ready）是 Pi 端已經正常回應、業務邏輯
拒絕，不算連線失敗，不重試。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from marvin_voice_core.puck_mixer_client import PuckMixerClient


class _FakeResp:
    def __init__(self, status=200, json_data=None):
        self.status = status
        self._json = json_data or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._json


class _FakeSession:
    """一次 `async with aiohttp.ClientSession(...)` 對應一個 _FakeSession 實例；
    raise_exc 設了就在呼叫 post()/get() 時直接丟例外，模擬連線層失敗。"""

    def __init__(self, resp: _FakeResp | None = None, raise_exc: Exception | None = None):
        self._resp = resp
        self._raise = raise_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, *a, **kw):
        if self._raise:
            raise self._raise
        return self._resp

    def get(self, *a, **kw):
        if self._raise:
            raise self._raise
        return self._resp


@pytest.mark.asyncio
async def test_post_retries_once_on_connection_failure_then_succeeds():
    """第一次連線層失敗，第二次成功 → 整體回 True，不是直接放棄。"""
    client = PuckMixerClient("http://pi.local:8766", token="tok")
    sessions = [
        _FakeSession(raise_exc=ConnectionError("boom")),
        _FakeSession(resp=_FakeResp(status=200)),
    ]
    with patch("marvin_voice_core.puck_mixer_client.aiohttp.ClientSession", side_effect=sessions), \
         patch("marvin_voice_core.puck_mixer_client.asyncio.sleep", new=AsyncMock()):
        ok = await client.play("https://ex/song")

    assert ok is True


@pytest.mark.asyncio
async def test_post_gives_up_after_exhausting_retries():
    """連續兩次都連線層失敗 → 回 False，不會無限重試。"""
    client = PuckMixerClient("http://pi.local:8766", token="tok")
    sessions = [
        _FakeSession(raise_exc=ConnectionError("boom")),
        _FakeSession(raise_exc=ConnectionError("boom again")),
    ]
    with patch("marvin_voice_core.puck_mixer_client.aiohttp.ClientSession", side_effect=sessions), \
         patch("marvin_voice_core.puck_mixer_client.asyncio.sleep", new=AsyncMock()) as fake_sleep:
        ok = await client.queue_next("https://ex/next")

    assert ok is False
    fake_sleep.assert_awaited_once()  # 只在兩次嘗試之間睡一次，不是每次都睡


@pytest.mark.asyncio
async def test_non_200_response_is_not_retried():
    """Pi 端有正常回應但狀態碼非 200（業務邏輯拒絕，例如 deck_b 還沒 ready）
    → 直接回 False，不重試（只建一次 session）。"""
    client = PuckMixerClient("http://pi.local:8766", token="tok")
    sessions = [_FakeSession(resp=_FakeResp(status=409))]
    with patch("marvin_voice_core.puck_mixer_client.aiohttp.ClientSession", side_effect=sessions) as fake_cls, \
         patch("marvin_voice_core.puck_mixer_client.asyncio.sleep", new=AsyncMock()) as fake_sleep:
        ok = await client.crossfade(4.0)

    assert ok is False
    assert fake_cls.call_count == 1
    fake_sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_retries_once_on_connection_failure_then_succeeds():
    """status() 的 GET 也走同一套重試。"""
    client = PuckMixerClient("http://pi.local:8766", token="tok")
    sessions = [
        _FakeSession(raise_exc=TimeoutError("slow")),
        _FakeSession(resp=_FakeResp(status=200, json_data={"playing": "x"})),
    ]
    with patch("marvin_voice_core.puck_mixer_client.aiohttp.ClientSession", side_effect=sessions), \
         patch("marvin_voice_core.puck_mixer_client.asyncio.sleep", new=AsyncMock()):
        result = await client.status()

    assert result == {"playing": "x"}

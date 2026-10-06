"""
tests/test_car_device_speaker.py
TDD：車載裝置自帶身分（方案 A：同一電台、分身分）。

- parse_device_speakers / resolve_device_speaker：白名單解析（純函式）。
- POST /car、POST /audio：裝置送的 speaker 經白名單驗證，未知身分 400。
- inject_audio(speaker=...)：轉錄後用指定身分注入，不再固定讀環境變數。

HTTP 層用 aiohttp TestServer；inject_text / inject_audio 用 monkeypatch 換成 AsyncMock，
好直接看「傳進去的身分」。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


DEFAULT = "狗與露"


def _make_vc():
    vc = MagicMock()
    vc.handle_stt_result = AsyncMock()
    vc.bot.cogs.get.return_value = None
    return vc


# ── parse_device_speakers ───────────────────────────────────────────────────
def test_parse_device_speakers_strips_and_skips_blanks():
    from main_satellite import parse_device_speakers
    out = parse_device_speakers("狗與露, showay ,,", DEFAULT)
    assert out == {"狗與露": "狗與露", "showay": "showay"}


def test_parse_device_speakers_always_includes_default():
    from main_satellite import parse_device_speakers
    out = parse_device_speakers("showay", DEFAULT)
    assert out == {"狗與露": "狗與露", "showay": "showay"}


def test_parse_device_speakers_keys_are_casefolded():
    from main_satellite import parse_device_speakers
    out = parse_device_speakers("ShowAy", DEFAULT)
    assert out["showay"] == "ShowAy"


# ── resolve_device_speaker ──────────────────────────────────────────────────
def test_resolve_allowed_none_ignores_device_value():
    from main_satellite import resolve_device_speaker
    assert resolve_device_speaker("showay", None, DEFAULT) == DEFAULT


@pytest.mark.parametrize("raw", [None, "  "])
def test_resolve_missing_or_blank_falls_back_to_default(raw):
    from main_satellite import parse_device_speakers, resolve_device_speaker
    allowed = parse_device_speakers("showay", DEFAULT)
    assert resolve_device_speaker(raw, allowed, DEFAULT) == DEFAULT


def test_resolve_case_insensitive_returns_formal_name():
    from main_satellite import parse_device_speakers, resolve_device_speaker
    allowed = parse_device_speakers("showay", DEFAULT)
    assert resolve_device_speaker("Showay", allowed, DEFAULT) == "showay"


def test_resolve_unknown_returns_none():
    from main_satellite import parse_device_speakers, resolve_device_speaker
    allowed = parse_device_speakers("showay", DEFAULT)
    assert resolve_device_speaker("eve", allowed, DEFAULT) is None


# ── POST /car ───────────────────────────────────────────────────────────────
def _car_app(cp, device_speakers):
    from main_satellite import build_text_app, parse_device_speakers
    allowed = parse_device_speakers(device_speakers, DEFAULT)
    return build_text_app(_make_vc(), token="s3cret", car_presence=cp,
                          default_speaker=DEFAULT, device_speakers=allowed)


@pytest.mark.asyncio
async def test_car_present_with_speaker_registers_that_occupant():
    from aiohttp.test_utils import TestClient, TestServer
    from car_presence import CarPresence
    arrive, depart = AsyncMock(), AsyncMock()
    cp = CarPresence(on_arrive=arrive, on_depart=depart)
    app = _car_app(cp, "showay")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/car?t=s3cret",
                                 json={"state": "present", "speaker": "showay"})
        assert resp.status == 200
        body = await resp.json()
    assert body["occupants"] == ["showay"]
    assert body["present"] is True
    arrive.assert_awaited_once_with("showay")


@pytest.mark.asyncio
async def test_car_present_without_speaker_uses_default():
    from aiohttp.test_utils import TestClient, TestServer
    from car_presence import CarPresence
    arrive = AsyncMock()
    cp = CarPresence(on_arrive=arrive, on_depart=AsyncMock())
    app = _car_app(cp, "showay")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/car?t=s3cret", json={"state": "present"})
        assert resp.status == 200
    arrive.assert_awaited_once_with(DEFAULT)


@pytest.mark.asyncio
async def test_car_unknown_speaker_400_and_presence_untouched():
    from aiohttp.test_utils import TestClient, TestServer
    from car_presence import CarPresence
    arrive = AsyncMock()
    cp = CarPresence(on_arrive=arrive, on_depart=AsyncMock())
    app = _car_app(cp, "showay")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/car?t=s3cret",
                                 json={"state": "present", "speaker": "eve"})
        assert resp.status == 400
        assert (await resp.json())["error"] == "unknown_speaker"
    arrive.assert_not_awaited()
    assert cp.is_present is False
    assert cp.occupants == []


@pytest.mark.asyncio
async def test_car_present_query_string_speaker():
    """非 JSON（query string）也要帶身分。"""
    from aiohttp.test_utils import TestClient, TestServer
    from car_presence import CarPresence
    cp = CarPresence(on_arrive=AsyncMock(), on_depart=AsyncMock())
    app = _car_app(cp, "showay")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/car?t=s3cret&state=present&speaker=showay")
        assert resp.status == 200
        assert (await resp.json())["occupants"] == ["showay"]


# ── POST /audio ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_audio_passes_device_speaker_to_inject(monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    import main_satellite
    fake = AsyncMock(return_value=True)
    monkeypatch.setattr(main_satellite, "inject_audio", fake)
    app = _car_app(None, "showay")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/audio?t=s3cret&speaker=showay", data=b"RIFFfake")
        assert resp.status == 200
    fake.assert_awaited_once()
    assert fake.call_args.kwargs["speaker"] == "showay"


@pytest.mark.asyncio
async def test_audio_unknown_speaker_400_and_no_inject(monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    import main_satellite
    fake = AsyncMock(return_value=True)
    monkeypatch.setattr(main_satellite, "inject_audio", fake)
    app = _car_app(None, "showay")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/audio?t=s3cret&speaker=eve", data=b"RIFFfake")
        assert resp.status == 400
        assert (await resp.json())["error"] == "unknown_speaker"
    fake.assert_not_awaited()


# ── inject_audio(speaker=...) ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_inject_audio_uses_given_speaker(monkeypatch):
    import main_satellite
    fake_inject_text = AsyncMock(return_value=True)
    monkeypatch.setattr(main_satellite, "inject_text", fake_inject_text)
    monkeypatch.setenv("MARVIN_SATELLITE_SPEAKER", DEFAULT)
    vc = MagicMock()
    vc.bot.engine._run_swift_stt = AsyncMock(return_value=("現在幾點", {}))
    ok = await main_satellite.inject_audio(vc, b"RIFFfake", speaker="showay")
    assert ok is True
    assert fake_inject_text.call_args.args[1] == "showay"


@pytest.mark.asyncio
async def test_inject_audio_without_speaker_falls_back_to_env(monkeypatch):
    import main_satellite
    fake_inject_text = AsyncMock(return_value=True)
    monkeypatch.setattr(main_satellite, "inject_text", fake_inject_text)
    monkeypatch.setenv("MARVIN_SATELLITE_SPEAKER", "阿凱")
    vc = MagicMock()
    vc.bot.engine._run_swift_stt = AsyncMock(return_value=("現在幾點", {}))
    await main_satellite.inject_audio(vc, b"RIFFfake")
    assert fake_inject_text.call_args.args[1] == "阿凱"

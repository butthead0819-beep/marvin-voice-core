"""TDD: shazam_identify — Shazam 音訊認歌（shazamio 非官方 API，失效退回呼叫端）。

測試不連網、不真的 import shazamio：全部注入 fake extract/recognize。
"""
from __future__ import annotations

import asyncio

import pytest

from shazam_identify import (
    BREAKER_COOLDOWN_S,
    BREAKER_FAILS,
    ShazamBreaker,
    clip_offset,
    identify,
    parse_track,
)


# ── parse_track ──────────────────────────────────────────────────────────────

def test_parse_track_normal_with_album():
    resp = {"track": {
        "title": "雙截棍", "subtitle": "周杰倫",
        "sections": [{"metadata": [{"title": "Album", "text": "范特西"}]}],
    }}
    assert parse_track(resp) == {"title": "雙截棍", "artist": "周杰倫", "album": "范特西"}


def test_parse_track_missing_track_key_returns_none():
    assert parse_track({}) is None
    assert parse_track(None) is None


def test_parse_track_empty_subtitle_returns_none():
    resp = {"track": {"title": "雙截棍", "subtitle": ""}}
    assert parse_track(resp) is None


def test_parse_track_no_album_metadata_returns_none_album():
    resp = {"track": {"title": "雙截棍", "subtitle": "周杰倫", "sections": []}}
    out = parse_track(resp)
    assert out["album"] is None


@pytest.mark.parametrize("album_title", ["Album", "專輯", "专辑"])
def test_parse_track_album_title_variants(album_title):
    resp = {"track": {
        "title": "晴天", "subtitle": "周杰倫",
        "sections": [{"metadata": [{"title": album_title, "text": "葉惠美"}]}],
    }}
    assert parse_track(resp)["album"] == "葉惠美"


# ── clip_offset ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("duration,expected", [
    (None, 60.0),
    (200, 60.0),
    (60, 18.0),
    (0, 60.0),
])
def test_clip_offset(duration, expected):
    assert clip_offset(duration) == expected


# ── ShazamBreaker ────────────────────────────────────────────────────────────

def test_breaker_trips_after_consecutive_fails_and_resets_after_cooldown():
    assert BREAKER_FAILS == 5 and BREAKER_COOLDOWN_S == 3600.0  # 定案值：連 5 次例外 → 停 1 小時
    b = ShazamBreaker()
    now = 1000.0
    for _ in range(BREAKER_FAILS):
        assert b.allow(now) is True
        b.record(False, now)
    assert b.allow(now) is False
    assert b.allow(now + BREAKER_COOLDOWN_S) is True


def test_breaker_success_in_between_resets_fail_count():
    b = ShazamBreaker()
    now = 1000.0
    for _ in range(BREAKER_FAILS - 1):
        b.record(False, now)
    b.record(True, now)
    for _ in range(BREAKER_FAILS - 1):
        b.record(False, now)
    assert b.allow(now) is True


# ── identify ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_identify_disabled_by_env_zero_extract_calls(monkeypatch):
    monkeypatch.setenv("MARVIN_SHAZAM", "0")
    extract_called = False

    async def _extract(url, offset):
        nonlocal extract_called
        extract_called = True
        return b"x" * 2000

    out = await identify("https://x", breaker=ShazamBreaker(), extract=_extract)
    assert out is None
    assert extract_called is False


@pytest.mark.asyncio
async def test_identify_breaker_tripped_zero_extract_calls():
    b = ShazamBreaker()
    b._resume_at = 10 ** 12  # 遠未來，模擬斷路中
    extract_called = False

    async def _extract(url, offset):
        nonlocal extract_called
        extract_called = True
        return b"x" * 2000

    out = await identify("https://x", breaker=b, extract=_extract)
    assert out is None
    assert extract_called is False


@pytest.mark.asyncio
async def test_identify_extract_none_returns_none_and_not_counted_as_failure():
    b = ShazamBreaker()

    async def _extract(url, offset):
        return None

    out = await identify("https://x", breaker=b, extract=_extract)
    assert out is None
    assert b._fail_count == 0
    assert b._resume_at is None


@pytest.mark.asyncio
async def test_identify_recognize_raises_counts_as_failure_and_trips_after_five():
    b = ShazamBreaker()

    async def _extract(url, offset):
        return b"x" * 2000

    async def _recognize(data):
        raise RuntimeError("shazamio 掛了")

    for _ in range(BREAKER_FAILS):
        out = await identify("https://x", breaker=b, extract=_extract, recognize=_recognize)
        assert out is None
    assert b._resume_at is not None
    assert b.allow(0.0) is False


@pytest.mark.asyncio
async def test_identify_recognize_empty_dict_counts_as_success_returns_none():
    b = ShazamBreaker()

    async def _extract(url, offset):
        return b"x" * 2000

    async def _recognize(data):
        return {}

    out = await identify("https://x", breaker=b, extract=_extract, recognize=_recognize)
    assert out is None
    assert b._fail_count == 0


@pytest.mark.asyncio
async def test_identify_normal_returns_parsed_track():
    b = ShazamBreaker()

    async def _extract(url, offset):
        return b"x" * 2000

    async def _recognize(data):
        return {"track": {"title": "雙截棍", "subtitle": "周杰倫"}}

    out = await identify("https://x", breaker=b, extract=_extract, recognize=_recognize)
    assert out == {"title": "雙截棍", "artist": "周杰倫", "album": None}


@pytest.mark.asyncio
async def test_identify_recognize_import_error_not_counted_as_failure():
    b = ShazamBreaker()

    async def _extract(url, offset):
        return b"x" * 2000

    async def _recognize(data):
        raise ImportError("no module named shazamio")

    out = await identify("https://x", breaker=b, extract=_extract, recognize=_recognize)
    assert out is None
    assert b._fail_count == 0
    assert b._resume_at is None


@pytest.mark.asyncio
async def test_identify_passes_duration_based_offset_to_extract():
    b = ShazamBreaker()
    captured = {}

    async def _extract(url, offset):
        captured["offset"] = offset
        return None

    await identify("https://x", duration=60, breaker=b, extract=_extract)
    assert captured["offset"] == 18.0


# ── _extract_clip ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_extract_clip_builds_ffmpeg_args_no_tempfile(monkeypatch):
    from shazam_identify import _extract_clip

    captured = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return b"x" * 2000, b""

    async def _fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    out = await _extract_clip("https://stream/url", 18.0)
    assert out == b"x" * 2000
    args = captured["args"]
    assert "-ss" in args and "18.0" in args
    assert "-t" in args and "12" in args
    # flac：wav 寫進 pipe 沒法回填檔頭長度，shazamio 解碼端會狂噴「skipping junk」灌爆 stdout log
    assert args[args.index("-f") + 1] == "flac"
    assert args[-1] == "-"
    assert captured["kwargs"]["stdout"] is asyncio.subprocess.PIPE


@pytest.mark.asyncio
async def test_extract_clip_nonzero_returncode_returns_none(monkeypatch):
    from shazam_identify import _extract_clip

    class _FakeProc:
        returncode = 1

        async def communicate(self):
            return b"", b"ffmpeg error"

    async def _fake_create_subprocess_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    out = await _extract_clip("https://stream/url", 18.0)
    assert out is None


@pytest.mark.asyncio
async def test_extract_clip_kills_ffmpeg_when_cancelled(monkeypatch):
    """identify 逾時會取消 _extract_clip——ffmpeg 子程序要被 kill，不能留孤兒在背景繼續下載。"""
    from shazam_identify import _extract_clip

    state = {"killed": False, "waited": False}

    class _HangingProc:
        returncode = None
        async def communicate(self):
            await asyncio.sleep(3600)
        def kill(self):
            state["killed"] = True
        async def wait(self):
            state["waited"] = True
            return -9

    async def _fake_create_subprocess_exec(*args, **kwargs):
        return _HangingProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(_extract_clip("https://stream/url", 18.0), 0.05)
    assert state["killed"] and state["waited"]

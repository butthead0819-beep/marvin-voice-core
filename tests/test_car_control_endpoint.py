"""
tests/test_car_control_endpoint.py

TDD：GET /car_control — 人手動下指令給 ESP32 car puck（寫進 puck_command_queue，
ESP32 下次輪詢 /car_commands 才會撿到，見 main_satellite.py::handle_car_control）。

驗：
(a) puck_command_queue=None（功能未開）→ 404
(b) ?cmd=play&url=... → 200，指令真的進了佇列
(c) ?cmd=play 缺 url → 400
(d) ?cmd=stop → 200，指令真的進了佇列
(e) ?cmd=bad → 400
(f) 也吃既有 token middleware：無 token → 401
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from marvin_voice_core.puck_command_queue import PuckCommandQueue


def _make_vc():
    vc = MagicMock()
    vc.bot.cogs.get.return_value = None
    return vc


@pytest.mark.asyncio
async def test_car_control_404_when_not_wired():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    app = build_text_app(_make_vc(), token="s3cret")   # 無 puck_command_queue
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_control?cmd=stop&t=s3cret")
        assert resp.status == 404


@pytest.mark.asyncio
async def test_car_control_play_enqueues_command():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    q = PuckCommandQueue()
    app = build_text_app(_make_vc(), token="s3cret", puck_command_queue=q)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_control?cmd=play&url=https://youtu.be/a&t=s3cret")
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True
        assert body["cmd"] == "play"

    seq, pending = q.since(0)
    assert [c["cmd"] for c in pending] == ["play"]
    assert pending[0]["url"] == "https://youtu.be/a"


@pytest.mark.asyncio
async def test_car_control_play_missing_url_400():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    q = PuckCommandQueue()
    app = build_text_app(_make_vc(), token="s3cret", puck_command_queue=q)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_control?cmd=play&t=s3cret")
        assert resp.status == 400


@pytest.mark.asyncio
async def test_car_control_stop_enqueues_command():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    q = PuckCommandQueue()
    app = build_text_app(_make_vc(), token="s3cret", puck_command_queue=q)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_control?cmd=stop&t=s3cret")
        assert resp.status == 200

    seq, pending = q.since(0)
    assert [c["cmd"] for c in pending] == ["stop"]


@pytest.mark.asyncio
async def test_car_control_bad_cmd_400():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    q = PuckCommandQueue()
    app = build_text_app(_make_vc(), token="s3cret", puck_command_queue=q)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_control?cmd=teleport&t=s3cret")
        assert resp.status == 400


@pytest.mark.asyncio
async def test_car_control_token_gated():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    q = PuckCommandQueue()
    app = build_text_app(_make_vc(), token="s3cret", puck_command_queue=q)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_control?cmd=stop")   # 無 token
        assert resp.status == 401


def _make_vc_with_current_stream(info: dict | None, *, start_time=None):
    """/car_now 讀的是本地 MusicCog 的即時屬性（_current_stream_info /
    _current_stream_start_time），不是 puck_command_queue——STEP10 韌體 deck A 固定吃
    /audio_stream，這份 MusicCog 才是真的餵進 /audio_stream 的來源。"""
    vc = MagicMock()
    music_cog = MagicMock()
    music_cog._current_stream_info = info
    music_cog._current_stream_start_time = start_time
    vc.bot.cogs.get.return_value = music_cog
    return vc


@pytest.mark.asyncio
async def test_car_now_playing_false_when_no_music_cog():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    app = build_text_app(_make_vc(), token="s3cret")   # cogs.get 回 None
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_now?t=s3cret")
        assert resp.status == 200
        body = await resp.json()
        assert body["playing"] is False


@pytest.mark.asyncio
async def test_car_now_playing_false_when_nothing_streaming():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    app = build_text_app(_make_vc_with_current_stream(None), token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_now?t=s3cret")
        body = await resp.json()
        assert body["playing"] is False


@pytest.mark.asyncio
async def test_car_now_reflects_local_music_cog_stream_info():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    info = {
        "title": "測試歌曲", "requested_by": "狗與露",
        "thumbnail": "https://img/a.jpg", "palette": ["#111", "#222"],
        "duration": 210,
    }
    app = build_text_app(_make_vc_with_current_stream(info, start_time=123.0), token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_now?t=s3cret")
        assert resp.status == 200
        body = await resp.json()
        assert body["playing"] is True
        assert body["title"] == "測試歌曲"
        assert body["by"] == "狗與露"
        assert body["cover"] == "https://img/a.jpg"
        assert body["palette"] == ["#111", "#222"]
        assert body["duration"] == 210
        assert body["song_start_time"] == 123.0


@pytest.mark.asyncio
async def test_car_now_includes_artist_and_album():
    """2026-08-21 車機要求顯示演出者/專輯——artist 優先讀 info['artist']，沒有就退回
    info['uploader']（yt-dlp 常見欄位）；album 缺欄位回空字串，不是 None。"""
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    info = {
        "title": "測試歌曲", "requested_by": "狗與露", "uploader": "某頻道",
        "artist": "某歌手", "album": "某專輯",
    }
    app = build_text_app(_make_vc_with_current_stream(info), token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_now?t=s3cret")
        body = await resp.json()
        assert body["artist"] == "某歌手"
        assert body["album"] == "某專輯"


@pytest.mark.asyncio
async def test_car_now_artist_falls_back_to_uploader_album_defaults_empty():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    info = {"title": "測試歌曲", "requested_by": "狗與露", "uploader": "某頻道"}
    app = build_text_app(_make_vc_with_current_stream(info), token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_now?t=s3cret")
        body = await resp.json()
        assert body["artist"] == "某頻道"
        assert body["album"] == ""


@pytest.mark.asyncio
async def test_car_now_token_gated():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    app = build_text_app(_make_vc(), token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_now")   # 無 token
        assert resp.status == 401

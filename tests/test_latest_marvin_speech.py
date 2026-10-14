"""
tests/test_latest_marvin_speech.py
/stage Marvin 頭的字幕泡泡＝marvin_speech.log 最新一句 DJ 串場/TTS（不含 ack、不含 song、只認 Discord）。
"""
import json

import pytest
from unittest.mock import AsyncMock, MagicMock


def _line(**kw):
    return json.dumps(kw, ensure_ascii=False)


def test_picks_latest_dj_or_tts_skipping_ack_and_song():
    from car_http_app import latest_marvin_speech
    lines = [
        _line(start=1.0, src="dj", text="舊的串場", origin="discord"),
        _line(start=2.0, src="tts", text="最近有點安靜", origin="discord"),
        _line(start=3.0, src="ack", text="嗯嗯。", origin="discord"),
        _line(start=4.0, src="song", text="倒帶", origin="discord"),
    ]
    assert latest_marvin_speech(lines) == {"text": "最近有點安靜", "src": "tts", "start": 2.0}


def test_skips_non_discord_origin_and_garbage_lines():
    from car_http_app import latest_marvin_speech
    lines = [
        _line(start=1.0, src="dj", text="家裡講的", origin="discord"),
        "{壞掉的半行",
        _line(start=2.0, src="tts", text="車上講的", origin="local"),
        "",
    ]
    assert latest_marvin_speech(lines) == {"text": "家裡講的", "src": "dj", "start": 1.0}


def test_none_when_nothing_spoken():
    from car_http_app import latest_marvin_speech
    assert latest_marvin_speech([]) is None
    assert latest_marvin_speech([_line(start=1.0, src="ack", text="我在聽。", origin="discord")]) is None


def _make_vc():
    vc = MagicMock()
    vc.handle_stt_result = AsyncMock()
    vc.bot.cogs.get.return_value = None
    return vc


@pytest.mark.asyncio
async def test_now_includes_latest_speech(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    import car_http_app
    log = tmp_path / "marvin_speech.log"
    log.write_text(_line(start=5.0, src="dj", text="這首給下班的你", origin="discord") + "\n", encoding="utf-8")
    monkeypatch.setattr(car_http_app, "_SPEECH_LOG_PATH", str(log))
    app = car_http_app.build_text_app(_make_vc(), token=None, now_playing_state_path=str(tmp_path / "none.json"))
    async with TestClient(TestServer(app)) as client:
        j = await (await client.get("/now")).json()
    assert j["playing"] is False
    assert j["speech"] == {"text": "這首給下班的你", "src": "dj", "start": 5.0}


@pytest.mark.asyncio
async def test_now_speech_none_when_log_missing(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    import car_http_app
    monkeypatch.setattr(car_http_app, "_SPEECH_LOG_PATH", str(tmp_path / "nope.log"))
    app = car_http_app.build_text_app(_make_vc(), token=None, now_playing_state_path=str(tmp_path / "none.json"))
    async with TestClient(TestServer(app)) as client:
        j = await (await client.get("/now")).json()
    assert j["speech"] is None

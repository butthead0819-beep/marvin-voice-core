"""
tests/test_now_endpoint_bpm.py
TDD：GET /now 的 bpm 欄位 — HUD 頭像律動（點頭/搖擺）要靠這個欄位判斷。

跨進程橋接檔（now_playing_state.json）帶 video_id，/now 查 _SONG_BPM_STORE
（跟 cogs/music_cog_audio_meta.py 寫 BPM 估計同一份檔）換成 bpm 數字；
查不到就回 None，不讓 HUD 誤判。
"""
import json

import pytest

from now_playing_state import save_now_playing_state


def _make_vc(mc=None):
    from unittest.mock import AsyncMock, MagicMock
    vc = MagicMock()
    vc.handle_stt_result = AsyncMock()
    vc.bot.cogs.get.return_value = mc
    return vc


@pytest.mark.asyncio
async def test_now_returns_bpm_when_store_has_video_id(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    import car_http_app
    from car_http_app import build_text_app

    bpm_store_path = str(tmp_path / "song_bpm.json")
    bpm_store_path_obj = tmp_path / "song_bpm.json"
    bpm_store_path_obj.write_text(json.dumps({"abcdefghijk": {"bpm": 128.0, "ts": 1}}), encoding="utf-8")
    monkeypatch.setattr(car_http_app, "_SONG_BPM_STORE", bpm_store_path)

    now_path = str(tmp_path / "now_playing_state.json")
    save_now_playing_state(playing=True, title="夜曲", by="大肚", video_id="abcdefghijk", path=now_path)

    app = build_text_app(_make_vc(mc=None), token=None, now_playing_state_path=now_path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/now")
        body = await resp.json()
        assert body["bpm"] == 128.0


@pytest.mark.asyncio
async def test_now_returns_none_bpm_when_video_id_not_in_store(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    import car_http_app
    from car_http_app import build_text_app

    bpm_store_path = str(tmp_path / "song_bpm.json")
    (tmp_path / "song_bpm.json").write_text(json.dumps({"abcdefghijk": {"bpm": 128.0, "ts": 1}}), encoding="utf-8")
    monkeypatch.setattr(car_http_app, "_SONG_BPM_STORE", bpm_store_path)

    now_path = str(tmp_path / "now_playing_state.json")
    save_now_playing_state(playing=True, title="夜曲", by="大肚", video_id="zzzzzzzzzzz", path=now_path)

    app = build_text_app(_make_vc(mc=None), token=None, now_playing_state_path=now_path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/now")
        body = await resp.json()
        assert body["bpm"] is None

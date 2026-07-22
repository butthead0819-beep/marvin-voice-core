"""TDD: DJ 環境行城市名接上 GPS 訊號（gps_context.city_label）。

沒有新鮮 GPS 訊號 → 退回家裡預設「台中」；車上 ESP32 puck 有新鮮訊號 → 用真實區名。
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/dj_audio.opus")
    bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)
    bot.router = MagicMock()
    bot.router.generate_dynamic_system_msg = AsyncMock(return_value="這首接得剛好")
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    bot.engine.post_summon_callback = None
    bot.music_memory = MagicMock()
    bot.music_memory._key = MagicMock(return_value="song_key_xyz")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="深夜")

    from cogs.music_cog import MusicCog
    return MusicCog(bot)


def _info():
    return {
        "title": "周杰倫 - 夜曲",
        "uploader": "周杰倫",
        "requested_by": "大肚",
        "url": "https://example/x",
    }


def _ctx_str(cog):
    call = cog.bot.router.generate_dynamic_system_msg.call_args
    return call.kwargs.get("context", "") or (call.args[1] if len(call.args) > 1 else "")


@pytest.mark.asyncio
async def test_no_gps_signal_falls_back_to_taichung(monkeypatch):
    import location_state
    monkeypatch.setattr(location_state, "load_location_state", lambda *a, **kw: None)
    cog = _make_cog()
    await cog._fetch_dj_interjection_raw(_info())
    ctx = _ctx_str(cog)
    assert "台中" in ctx


@pytest.mark.asyncio
async def test_fresh_car_gps_overrides_city_in_environment_line(monkeypatch):
    import location_state
    now = time.time()
    monkeypatch.setattr(
        location_state, "load_location_state",
        lambda *a, **kw: {"lat": 25.0693, "lon": 121.5885, "ts": now},
    )
    cog = _make_cog()
    await cog._fetch_dj_interjection_raw(_info())
    ctx = _ctx_str(cog)
    assert "內湖區" in ctx
    assert "環境：內湖區" in ctx

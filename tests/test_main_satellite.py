"""
tests/test_main_satellite.py
TDD 先紅後綠：驗 main_satellite.py wiring（無 .env / 無硬體 / 無網路 / 不登入 Discord）。
mirror test_main_local.py，差異＝呼叫 start_satellite_listening。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock


def _make_mock_bot():
    mock_vc = MagicMock()
    mock_vc.start_satellite_listening = MagicMock()

    bot = MagicMock()
    bot.load_extension = AsyncMock()
    bot.engine = MagicMock()
    bot.engine.start = MagicMock()
    bot.cogs = MagicMock()
    bot.cogs.get = MagicMock(return_value=mock_vc)
    bot.start = AsyncMock()
    bot.run = MagicMock()
    bot.login = AsyncMock()
    bot.connect = AsyncMock()
    return bot, mock_vc


def test_import_main_satellite_succeeds():
    import main_satellite
    assert hasattr(main_satellite, "setup_satellite")
    assert hasattr(main_satellite, "build_local_bot")


@pytest.mark.asyncio
async def test_setup_satellite_loads_voice_controller_cog(monkeypatch):
    monkeypatch.delenv("MARVIN_CAR_HARDWARE", raising=False)
    from main_satellite import setup_satellite
    bot, _ = _make_mock_bot()
    await setup_satellite(bot)
    loaded = [c.args[0] for c in bot.load_extension.call_args_list]
    assert "cogs.voice_controller" in loaded


@pytest.mark.asyncio
async def test_setup_satellite_loads_music_cog_before_voice_controller(monkeypatch):
    monkeypatch.delenv("MARVIN_CAR_HARDWARE", raising=False)
    from main_satellite import setup_satellite
    bot, _ = _make_mock_bot()
    await setup_satellite(bot)
    loaded = [c.args[0] for c in bot.load_extension.call_args_list]
    assert "cogs.music_cog" in loaded
    assert loaded.index("cogs.music_cog") < loaded.index("cogs.voice_controller")


@pytest.mark.asyncio
async def test_setup_satellite_calls_start_satellite_listening(monkeypatch):
    monkeypatch.delenv("MARVIN_CAR_HARDWARE", raising=False)
    from main_satellite import setup_satellite
    bot, mock_vc = _make_mock_bot()
    await setup_satellite(bot)
    mock_vc.start_satellite_listening.assert_called_once_with()


@pytest.mark.asyncio
async def test_setup_satellite_never_logs_into_discord(monkeypatch):
    monkeypatch.delenv("MARVIN_CAR_HARDWARE", raising=False)
    from main_satellite import setup_satellite
    bot, _ = _make_mock_bot()
    await setup_satellite(bot)
    bot.start.assert_not_called()
    bot.run.assert_not_called()
    bot.login.assert_not_called()
    bot.connect.assert_not_called()


@pytest.mark.asyncio
async def test_setup_satellite_without_pi_bt_returns_no_stream_source(monkeypatch):
    monkeypatch.delenv("MARVIN_CAR_HARDWARE", raising=False)
    from main_satellite import setup_satellite
    bot, mock_vc = _make_mock_bot()
    vc, stream_out = await setup_satellite(bot)
    assert vc is mock_vc
    assert stream_out is None


# ── MARVIN_CAR_HARDWARE=pi_bt：接 StreamSpeakerOutput，車 puck 跟家用喇叭共用同一顆
#    mixer（見 setup_satellite docstring，2026-08-20 換歌決策改回跟 ESP32 一樣走
#    /audio_stream「收音機」模式，不再是 Mac 送 play/queue_next/crossfade 指令）──

@pytest.mark.asyncio
async def test_setup_satellite_pi_bt_wires_stream_speaker_output(monkeypatch):
    monkeypatch.setenv("MARVIN_CAR_HARDWARE", "pi_bt")
    from marvin_voice_core.stream_speaker_output import StreamSpeakerOutput
    from main_satellite import setup_satellite
    bot, mock_vc = _make_mock_bot()

    vc, stream_out = await setup_satellite(bot)

    assert vc is mock_vc
    assert isinstance(stream_out, StreamSpeakerOutput)
    mock_vc.start_satellite_listening.assert_called_once_with(extra_output=stream_out)


@pytest.mark.asyncio
async def test_setup_satellite_pi_bt_arms_mixer_immediately(monkeypatch):
    """開機立刻 arm 泵讓靜音幀先流動——比照 setup_browser_satellite 車載模式的既有理由
    （車 puck 一連上 /audio_stream 就有東西可讀，不用等第一句話/第一首歌才出聲）。"""
    monkeypatch.setenv("MARVIN_CAR_HARDWARE", "pi_bt")
    from main_satellite import setup_satellite
    bot, mock_vc = _make_mock_bot()

    await setup_satellite(bot)

    mock_vc._ensure_mixer_playing.assert_called_once_with(mock_vc._resolve_playback_device())

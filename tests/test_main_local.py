"""
tests/test_main_local.py
TDD 先紅後綠：驗 main_local.py wiring（無 .env / 無硬體 / 無網路）。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock


def _make_mock_bot():
    mock_vc = MagicMock()
    mock_vc.start_local_listening = MagicMock()

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


def test_import_main_local_succeeds():
    import main_local
    assert hasattr(main_local, "setup_local")
    assert hasattr(main_local, "build_local_bot")


@pytest.mark.asyncio
async def test_setup_local_loads_voice_controller_cog():
    from main_local import setup_local
    bot, _ = _make_mock_bot()
    await setup_local(bot)
    loaded = [c.args[0] for c in bot.load_extension.call_args_list]
    assert "cogs.voice_controller" in loaded


@pytest.mark.asyncio
async def test_setup_local_loads_music_cog_before_voice_controller():
    from main_local import setup_local
    bot, _ = _make_mock_bot()
    await setup_local(bot)
    loaded = [c.args[0] for c in bot.load_extension.call_args_list]
    assert "cogs.music_cog" in loaded
    assert loaded.index("cogs.music_cog") < loaded.index("cogs.voice_controller")


@pytest.mark.asyncio
async def test_setup_local_calls_start_local_listening():
    from main_local import setup_local
    bot, mock_vc = _make_mock_bot()
    await setup_local(bot)
    mock_vc.start_local_listening.assert_called_once()


@pytest.mark.asyncio
async def test_setup_local_never_logs_into_discord():
    from main_local import setup_local
    bot, _ = _make_mock_bot()
    await setup_local(bot)
    bot.start.assert_not_called()
    bot.run.assert_not_called()
    bot.login.assert_not_called()
    bot.connect.assert_not_called()

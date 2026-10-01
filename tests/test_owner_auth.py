"""測試營運者 (owner) 身分判定與權限防護。

涵蓋：
- owner_auth.py: is_owner / owner_id / OWNER_ONLY_MESSAGE
- cogs/voice_controller.py: marvin_reboot check
- main_discord.py: on_app_command_error 處理 CheckFailure
- cogs/voice_controller_connection.py: self_restart(pull=True) 不再在文字頻道貼 git pull
"""
import asyncio
import os
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import discord
from discord import app_commands

from owner_auth import OWNER_ONLY_MESSAGE, _DEFAULT_OWNER_ID, is_owner, owner_id


def test_is_owner_marvin_owner_id_precedence(monkeypatch):
    monkeypatch.setenv("MARVIN_OWNER_ID", "111")
    monkeypatch.setenv("LOCAL_USER_ID", "333")
    assert is_owner(111) is True
    assert is_owner(222) is False
    assert is_owner("111") is True


def test_is_owner_local_user_id_fallback(monkeypatch):
    monkeypatch.delenv("MARVIN_OWNER_ID", raising=False)
    monkeypatch.setenv("LOCAL_USER_ID", "333")
    assert is_owner(333) is True
    assert is_owner(111) is False


def test_is_owner_default_id(monkeypatch):
    monkeypatch.delenv("MARVIN_OWNER_ID", raising=False)
    monkeypatch.delenv("LOCAL_USER_ID", raising=False)
    assert is_owner(_DEFAULT_OWNER_ID) is True
    assert is_owner(int(_DEFAULT_OWNER_ID)) is True
    assert is_owner(99999) is False


def test_is_owner_invalid_inputs():
    assert is_owner(None) is False
    assert is_owner("abc") is False


def test_marvin_reboot_has_owner_check(monkeypatch):
    monkeypatch.setenv("MARVIN_OWNER_ID", "12345")
    from cogs.voice_controller import VoiceController

    assert hasattr(VoiceController, "marvin_reboot")
    checks = VoiceController.marvin_reboot.checks
    assert len(checks) >= 1

    check = checks[0]
    owner_interaction = MagicMock(user=MagicMock(id=12345))
    non_owner_interaction = MagicMock(user=MagicMock(id=99999))

    assert check(owner_interaction) is True
    assert check(non_owner_interaction) is False


@pytest.mark.asyncio
async def test_on_app_command_error_check_failure(monkeypatch):
    import main_discord

    handler = None
    for const in main_discord.MarvinBot.setup_hook.__code__.co_consts:
        if isinstance(const, types.CodeType) and const.co_name == "on_app_command_error":
            handler = types.FunctionType(const, main_discord.__dict__)
            break
    assert handler is not None, "找不到 on_app_command_error"

    mock_logger = MagicMock()
    monkeypatch.setattr(main_discord, "logger", mock_logger)

    interaction = MagicMock(spec=discord.Interaction)
    interaction.command = MagicMock()
    interaction.command.name = "marvin_reboot"
    interaction.user = "TestUser#1234"
    interaction.response = MagicMock()
    interaction.response.is_done.return_value = False
    interaction.response.send_message = AsyncMock()

    error = app_commands.CheckFailure()

    await handler(interaction, error)

    # 驗證回傳 ephemeral 的 OWNER_ONLY_MESSAGE
    interaction.response.send_message.assert_awaited_once_with(OWNER_ONLY_MESSAGE, ephemeral=True)
    # 驗證沒有呼叫 logger.error
    mock_logger.error.assert_not_called()
    # 驗證有呼叫 logger.info 記錄拒絕
    assert mock_logger.info.called


@pytest.mark.asyncio
async def test_self_restart_no_git_pull_in_text_channel(monkeypatch):
    from cogs.voice_controller_connection import ConnectionMixin

    class FakeController(ConnectionMixin):
        def __init__(self):
            self.bot = MagicMock()
            self.bot.close = AsyncMock()
            self.last_restart_time = 0
            self.active_text_channel = MagicMock()
            self.active_text_channel.send = AsyncMock()
            self.active_text_channel.id = 100
            self.active_text_channel.guild.id = 200

    controller = FakeController()

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(return_value=(b"Already up to date.", b""))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=fake_proc))
    monkeypatch.setattr(os, "execv", MagicMock())
    monkeypatch.setattr("cogs.voice_controller_connection._write_reboot_state", MagicMock())
    monkeypatch.setattr("cogs.voice_controller_connection._git_head_short", MagicMock(return_value="abc1234"))

    await controller.self_restart(reason="測試", force=True, pull=True)

    # 驗證 active_text_channel.send 沒有被送出含 "git pull" 的字串
    for call in controller.active_text_channel.send.mock_calls:
        args, kwargs = call[1], call[2]
        content = args[0] if args else kwargs.get("content", "")
        assert "git pull" not in content

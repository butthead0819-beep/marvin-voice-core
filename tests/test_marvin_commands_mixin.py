"""
MarvinCommandsMixin — 把 VoiceController 的「表演 / 觀察報告 / 系統診斷」slash
指令抽到獨立 mixin 檔（減肥 voice_controller.py），但仍以 mixin 形式併入
VoiceController，因此 self 身分不變、行為零改動。

這組測試守住兩件事：
  1. 結構：指令確實搬到 cogs.voice_controller_commands，且仍註冊在 VoiceController 上
  2. 行為：代表性指令（marvin_say / marvin_sing）的 play_tts 包裝與保護旗標還原不變
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from discord import app_commands


# ── 要搬到 mixin 的指令清單 ───────────────────────────────────────────────
MOVED_COMMANDS = [
    "marvin_bias",
    "marvin_sing",
    "marvin_joke",
    "marvin_say",
    "marvin_manzai",
    "marvin_imitate",
    "marvin_news",
    "marvin_standup",
    "marvin_status",
    "marvin_system",
]

# ── 必須留在 VoiceController（核心生命週期 / 控制面）的指令 ────────────────
STAYS_PUT = ["summon", "dismiss", "marvin_reboot", "marvin_tts_clear",
             "marvin_optin", "marvin_optout"]


def test_mixin_in_voice_controller_mro():
    from cogs.voice_controller import VoiceController
    from cogs.voice_controller_commands import MarvinCommandsMixin
    assert MarvinCommandsMixin in VoiceController.__mro__


@pytest.mark.parametrize("name", MOVED_COMMANDS)
def test_moved_command_is_registered_and_defined_in_mixin(name):
    from cogs.voice_controller import VoiceController
    cmd = getattr(VoiceController, name)
    assert isinstance(cmd, app_commands.Command), f"{name} 不是已註冊的 app_command"
    # callback 的定義模組必須是新 mixin 檔（證明真的搬了，不是還留在原檔）
    assert cmd.callback.__module__ == "cogs.voice_controller_commands"


@pytest.mark.parametrize("name", STAYS_PUT)
def test_lifecycle_command_stays_in_voice_controller(name):
    from cogs.voice_controller import VoiceController
    cmd = getattr(VoiceController, name)
    assert isinstance(cmd, app_commands.Command)
    assert cmd.callback.__module__ == "cogs.voice_controller"


def test_pool_status_helpers_moved_with_system_command():
    """marvin_system 專用的兩個 static helper 必須一起搬，避免留下孤兒。"""
    from cogs.voice_controller_commands import MarvinCommandsMixin
    assert hasattr(MarvinCommandsMixin, "_fmt_pool_status")
    assert hasattr(MarvinCommandsMixin, "_fmt_quality_today")


def _make_vc():
    """繞過 VoiceController 全建構，只測指令 callback 行為。"""
    from cogs.voice_controller import VoiceController
    vc = VoiceController.__new__(VoiceController)
    vc.play_tts = AsyncMock()
    vc._tts_protected = False
    vc._tts_interrupted = True
    vc.bot = MagicMock()
    return vc


def _make_interaction():
    interaction = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.user.display_name = "陳進文"
    interaction.channel = MagicMock()
    return interaction


@pytest.mark.asyncio
async def test_marvin_say_uses_macos_protected_and_restores_flag():
    from cogs.voice_controller import VoiceController
    vc = _make_vc()
    interaction = _make_interaction()

    await VoiceController.marvin_say.callback(vc, interaction, text="哈囉世界")

    vc.play_tts.assert_awaited_once()
    _, kwargs = vc.play_tts.call_args
    assert kwargs["force_macos"] is True
    assert kwargs["protected"] is True
    assert kwargs["already_in_channel"] is True
    # 保護旗標用完還原成原值（False），不 clobber
    assert vc._tts_protected is False
    # 進場前先清中斷旗標
    assert vc._tts_interrupted is False


@pytest.mark.asyncio
async def test_marvin_sing_announces_and_schedules_manual_request():
    from cogs.voice_controller import VoiceController
    import asyncio

    vc = _make_vc()
    vc.bot.router.generate_dynamic_system_msg = AsyncMock(return_value="哼，又要唱歌")
    vc.manual_sing_request = AsyncMock()
    interaction = _make_interaction()

    await VoiceController.marvin_sing.callback(vc, interaction, theme="生日快樂")
    # 給 create_task 一個 tick 跑起來
    await asyncio.sleep(0)

    interaction.followup.send.assert_awaited()
    vc.play_tts.assert_awaited_once()
    vc.manual_sing_request.assert_awaited_once()
    _, kwargs = vc.manual_sing_request.call_args
    assert kwargs["force_new"] is True
    assert kwargs["theme"] == "生日快樂"

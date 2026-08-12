"""
MarvinCommandsMixin — marvin_say slash 指令測試。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from discord import app_commands


# ── 要搬到 mixin 的指令清單 ───────────────────────────────────────────────
MOVED_COMMANDS = [
    "marvin_say",
]

# ── 必須留在 VoiceController（控制面）的指令 ──────────────────────────────────
# 註：summon / dismiss 已移至 ConnectionMixin（見 test_connection_mixin.py），不在此清單。
STAYS_PUT = ["marvin_reboot", "marvin_optin", "marvin_optout"]


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


@pytest.mark.asyncio
async def test_marvin_say_uses_macos_protected_and_restores_flag():
    from cogs.voice_controller import VoiceController
    vc = VoiceController.__new__(VoiceController)
    vc.play_tts = AsyncMock()
    vc._tts_protected = False
    vc._tts_interrupted = True
    vc.bot = MagicMock()
    vc.stt_logger = MagicMock()

    interaction = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.user.display_name = "陳進文"

    await VoiceController.marvin_say.callback(vc, interaction, text="哈囉世界")

    vc.play_tts.assert_awaited_once()
    _, kwargs = vc.play_tts.call_args
    assert kwargs["force_macos"] is True
    assert kwargs["protected"] is True
    assert kwargs["already_in_channel"] is True
    assert vc._tts_protected is False
    assert vc._tts_interrupted is False

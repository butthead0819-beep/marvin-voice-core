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
    "marvin_talk",
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


def _talk_interaction():
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.user.id = 42
    interaction.user.display_name = "陳進文"
    return interaction


@pytest.mark.asyncio
async def test_marvin_talk_refuses_when_not_in_voice():
    from cogs.voice_controller import VoiceController
    vc = VoiceController.__new__(VoiceController)
    vc.talk_manager = MagicMock()
    vc._voice_client_override = None  # voice_client property → None（未連線）
    vc.bot = MagicMock(voice_clients=[])
    interaction = _talk_interaction()

    await VoiceController.marvin_talk.callback(vc, interaction)

    interaction.response.send_message.assert_awaited_once()
    assert "summon" in interaction.response.send_message.call_args.args[0]
    vc.talk_manager.toggle.assert_not_called()


@pytest.mark.asyncio
async def test_talk_say_clears_interrupt_and_protects():
    """對談回覆出聲前必須清 _tts_interrupted + 拉 _tts_protected，
    否則使用者剛講完話 → 回覆被 [TTS Interrupt Guard]/[Silence Gate] 整段跳過（11:40 bug）。"""
    from cogs.voice_controller import VoiceController
    vc = VoiceController.__new__(VoiceController)
    seen = {}

    async def _capture(*a, **kw):
        seen["interrupted"] = vc._tts_interrupted
        seen["protected"] = vc._tts_protected

    vc.play_tts = AsyncMock(side_effect=_capture)
    vc._tts_interrupted = True
    vc._tts_protected = False

    await VoiceController._talk_say(vc, "隨便啦，你自己想。")

    assert seen["interrupted"] is False       # 呼叫當下已清中斷旗標
    assert seen["protected"] is True          # 呼叫當下受保護
    assert vc._tts_protected is False         # finally 還原原值
    _, kwargs = vc.play_tts.call_args
    assert kwargs["force_macos"] is True


def test_talk_music_pause_mutes_layer_not_whole_mixer():
    """對談暫停音樂只能壓音樂層音量，絕不能 _mixer.set_paused()——
    整台泵暫停會連馬文回覆 TTS 一起卡住（11:46 bug）。"""
    from cogs.voice_controller import VoiceController
    vc = VoiceController.__new__(VoiceController)
    vc._mixer = MagicMock()
    vc._mixer._volume_target = 0.8

    vc._talk_set_music_paused(True)
    vc._mixer.set_volume.assert_called_once_with(0.0)
    vc._mixer.set_paused.assert_not_called()

    vc._talk_set_music_paused(False)
    vc._mixer.set_volume.assert_called_with(0.8)


@pytest.mark.asyncio
async def test_marvin_talk_toggles_when_connected():
    from cogs.voice_controller import VoiceController
    vc = VoiceController.__new__(VoiceController)
    vc.talk_manager = MagicMock(toggle=AsyncMock(return_value="🎙️ 好，說話。"))
    vc._voice_client_override = MagicMock(is_connected=lambda: True)
    vc.bot = MagicMock()
    vc.stt_logger = MagicMock()
    interaction = _talk_interaction()

    await VoiceController.marvin_talk.callback(vc, interaction)

    vc.talk_manager.toggle.assert_awaited_once_with(42, "陳進文")
    interaction.followup.send.assert_awaited_once()

"""
ConnectionMixin — VoiceController 的連線生命週期 + 自癒（哨兵）方法抽到獨立檔
（減肥 voice_controller.py），以 mixin 併入，self 身分不變、行為零改動。

守：mixin 在 MRO、12 個方法搬到新模組、summon/dismiss 仍註冊 app_command、
sentinel_monitor_loop 仍是 tasks.Loop。
"""
from __future__ import annotations

import pytest
from discord import app_commands
from discord.ext import tasks

MOD = "cogs.voice_controller_connection"

PLAIN = [
    "report_sink_error",
    "handle_fallback_notification",
    "orchestrate_recovery",
    "soft_repair_connection",
    "auto_attach_listener",
    "handle_summon",
    "handle_dismiss",
    "self_restart",
    "_dave_grace_should_forgive",
]


def test_mixin_in_mro():
    from cogs.voice_controller import VoiceController
    from cogs.voice_controller_connection import ConnectionMixin
    assert ConnectionMixin in VoiceController.__mro__


@pytest.mark.parametrize("name", PLAIN)
def test_plain_method_moved(name):
    from cogs.voice_controller import VoiceController
    assert getattr(VoiceController, name).__module__ == MOD


@pytest.mark.parametrize("name", ["summon", "dismiss"])
def test_slash_command_moved_and_registered(name):
    from cogs.voice_controller import VoiceController
    cmd = getattr(VoiceController, name)
    assert isinstance(cmd, app_commands.Command)
    assert cmd.callback.__module__ == MOD


def test_sentinel_loop_moved_and_is_loop():
    from cogs.voice_controller import VoiceController
    loop = VoiceController.sentinel_monitor_loop
    assert isinstance(loop, tasks.Loop)
    assert loop.coro.__module__ == MOD


def test_echo_guard_stays_in_voice_controller():
    # _strong_voice_bypass_echo 是喚醒/echo guard（屬 STT），不該被當連線搬走
    from cogs.voice_controller import VoiceController
    assert VoiceController._strong_voice_bypass_echo.__module__ == "cogs.voice_controller"

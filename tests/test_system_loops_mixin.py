"""
SystemLoopsMixin — VoiceController 的週期性系統維護迴圈抽到獨立檔（減肥），
以 mixin 併入，self 身分不變、零行為改動。三個迴圈皆 @tasks.loop，由 cog_load
的 self.X.start() 經 MRO 正常啟動。
"""
from __future__ import annotations

import pytest
from discord.ext import tasks

MOD = "cogs.voice_controller_system_loops"
LOOPS = ["slow_system_loop", "daily_log_export_loop", "reset_stt_counter_loop"]


def test_mixin_in_mro():
    from cogs.voice_controller import VoiceController
    from cogs.voice_controller_system_loops import SystemLoopsMixin
    assert SystemLoopsMixin in VoiceController.__mro__


@pytest.mark.parametrize("name", LOOPS)
def test_loop_moved_and_is_loop(name):
    from cogs.voice_controller import VoiceController
    loop = getattr(VoiceController, name)
    assert isinstance(loop, tasks.Loop), f"{name} 不是 tasks.Loop"
    assert loop.coro.__module__ == MOD

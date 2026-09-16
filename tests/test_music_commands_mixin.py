"""
MusicCommandsMixin — 確認 6 個音樂 slash 指令真的搬進 mixin（非只是留在 music_cog.py
掛好 import 就通過）。比照 tests/test_marvin_commands_mixin.py 的驗證方式。
"""
from __future__ import annotations

import pytest
from discord import app_commands

MOVED_COMMANDS = [
    "marvin_radio",
    "marvin_play",
    "marvin_skip",
    "marvin_play_control",
    "marvin_playlist_export",
    "marvin_playlist_import",
]


def test_mixin_in_music_cog_mro():
    from cogs.music_cog import MusicCog
    from cogs.music_cog_commands import MusicCommandsMixin
    assert MusicCommandsMixin in MusicCog.__mro__


@pytest.mark.parametrize("name", MOVED_COMMANDS)
def test_moved_command_is_registered_and_defined_in_mixin(name):
    from cogs.music_cog import MusicCog
    cmd = getattr(MusicCog, name)
    assert isinstance(cmd, app_commands.Command), f"{name} 不是已註冊的 app_command"
    # callback 的定義模組必須是新 mixin 檔（證明真的搬了，不是還留在原檔）
    assert cmd.callback.__module__ == "cogs.music_cog_commands"

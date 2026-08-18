"""TDD — auto rejoin/soft repair 靜默回台時，文字卡片該貼回使用者平常在看的頻道，
不是退到語音頻道自帶的內建文字區（2026-08-19 使用者反映：auto rejoin 卡片有貼，但
貼到語音頻道內建文字 tab，不是他平常看的頻道，等於「都不會有」）。

修法：/summon 設定 active_text_channel 時，順手把該頻道 id 記錄到磁碟
（.marvin_last_text_channel.json）。auto_rejoin_on_boot() / soft_repair_connection()
靜默回台、active_text_channel 是 None 時，優先讀這份記錄還原成使用者的真實頻道，
讀不到（沒記錄過 / 頻道被刪）才退回語音頻道自帶文字區。
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import discord

from cogs.voice_controller_connection import (
    _read_last_text_channel,
    _write_last_text_channel,
)


def test_write_then_read_round_trip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import cogs.voice_controller_connection as vcc
    monkeypatch.setattr(vcc, "LAST_TEXT_CHANNEL_FILE", str(tmp_path / "last_ch.json"))

    channel = MagicMock()
    channel.id = 123
    channel.guild.id = 456
    _write_last_text_channel(channel)

    saved = json.loads((tmp_path / "last_ch.json").read_text())
    assert saved == {"guild_id": 456, "channel_id": 123}

    resolved = MagicMock(spec=discord.TextChannel)
    bot = MagicMock()
    bot.get_channel.return_value = resolved
    assert _read_last_text_channel(bot) is resolved
    bot.get_channel.assert_called_once_with(123)


def test_read_returns_none_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import cogs.voice_controller_connection as vcc
    monkeypatch.setattr(vcc, "LAST_TEXT_CHANNEL_FILE", str(tmp_path / "missing.json"))

    assert _read_last_text_channel(MagicMock()) is None


def test_read_returns_none_when_channel_not_text_channel(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import cogs.voice_controller_connection as vcc
    monkeypatch.setattr(vcc, "LAST_TEXT_CHANNEL_FILE", str(tmp_path / "last_ch.json"))
    (tmp_path / "last_ch.json").write_text(json.dumps({"guild_id": 1, "channel_id": 2}))

    bot = MagicMock()
    bot.get_channel.return_value = MagicMock(spec=discord.VoiceChannel)  # 語音頻道自己也有 id
    assert _read_last_text_channel(bot) is None

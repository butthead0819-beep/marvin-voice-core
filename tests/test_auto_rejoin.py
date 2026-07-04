"""TDD: 開機自動回台（2026-07-04）。

kickstart 後 bot 是離台狀態、要人手動 /summon——今晨四次部署重啟每次
把 Marvin 踢下台，10:06 後沒人再召 → 離台 1.5h 聽不到也貼不了漫畫。
規則：開機時任一語音頻道有真人 → 自動回台恢復監聽（安靜回歸，不打招呼）。
"""
from __future__ import annotations

from unittest.mock import MagicMock

from cogs.voice_controller_connection import pick_rejoin_channel


def _member(bot=False):
    m = MagicMock(); m.bot = bot; return m


def _guild(channels):
    g = MagicMock(); g.voice_channels = channels; return g


def test_picks_channel_with_humans():
    ch = MagicMock(); ch.members = [_member(bot=True), _member(bot=False)]
    assert pick_rejoin_channel([_guild([ch])], already_connected=False) is ch


def test_skips_bot_only_channel():
    ch = MagicMock(); ch.members = [_member(bot=True)]
    assert pick_rejoin_channel([_guild([ch])], already_connected=False) is None


def test_skips_empty_channels():
    ch = MagicMock(); ch.members = []
    assert pick_rejoin_channel([_guild([ch])], already_connected=False) is None


def test_noop_when_already_connected():
    ch = MagicMock(); ch.members = [_member()]
    assert pick_rejoin_channel([_guild([ch])], already_connected=True) is None


def test_noop_when_no_guilds():
    assert pick_rejoin_channel([], already_connected=False) is None
    assert pick_rejoin_channel(None, already_connected=False) is None

"""TDD — MarvinVoicePipeline speaker_provider 注入機制。

先紅（speaker_provider / _resolve_speaker 尚未存在時失敗），
實作後轉綠。

三類測試：
  (a) 預設路徑（None）命中 member → nick or display_name
  (b) 預設路徑（None）跨 guild 找不到 → User_<id> fallback
  (c) 注入 provider → 回傳 provider 的值（含 user_id='local'）
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_pipeline(speaker_provider=None, guilds=None):
    """建 MarvinVoicePipeline，stub 掉 STTHandler。"""
    bot = MagicMock()
    bot.guilds = guilds if guilds is not None else []
    bot.router = MagicMock(game_dict_string="")

    with patch("marvin_voice_core.pipeline.STTHandler", MagicMock()):
        from marvin_voice_core.pipeline import MarvinVoicePipeline
        pipe = MarvinVoicePipeline(bot, speaker_provider=speaker_provider)
    return pipe


# ── (a) 預設路徑：命中 member.nick ────────────────────────────────────────────

def test_default_resolve_returns_nick_when_nick_set():
    member = MagicMock()
    member.nick = "酷哥"
    member.display_name = "顯示名"

    guild = MagicMock()
    guild.get_member.return_value = member

    pipe = _make_pipeline(guilds=[guild])
    result = pipe._resolve_speaker(12345)

    assert result == "酷哥"
    guild.get_member.assert_called_once_with(12345)


def test_default_resolve_returns_display_name_when_nick_is_none():
    member = MagicMock()
    member.nick = None
    member.display_name = "顯示名"

    guild = MagicMock()
    guild.get_member.return_value = member

    pipe = _make_pipeline(guilds=[guild])
    result = pipe._resolve_speaker(99)

    assert result == "顯示名"


# ── (b) 預設路徑：找不到 → fallback ──────────────────────────────────────────

def test_default_resolve_fallback_no_guilds():
    pipe = _make_pipeline(guilds=[])
    result = pipe._resolve_speaker(999)
    assert result == "User_999"


def test_default_resolve_fallback_member_not_found():
    guild = MagicMock()
    guild.get_member.return_value = None

    pipe = _make_pipeline(guilds=[guild])
    result = pipe._resolve_speaker(42)
    assert result == "User_42"


def test_default_resolve_fallback_for_local_str_user_id():
    """user_id='local' 在預設路徑 get_member 回 None → 'User_local'。"""
    guild = MagicMock()
    guild.get_member.return_value = None

    pipe = _make_pipeline(guilds=[guild])
    result = pipe._resolve_speaker("local")
    assert result == "User_local"


# ── (c) 注入路徑：provider 覆蓋所有查名 ──────────────────────────────────────

def test_injected_provider_overrides_for_int_user_id():
    pipe = _make_pipeline(speaker_provider=lambda uid: "Jack")
    assert pipe._resolve_speaker(12345) == "Jack"


def test_injected_provider_overrides_for_local_str_user_id():
    """本機 LocalMicSink 的 user_id='local' 透過 provider 映射到既有 Discord 名字。"""
    pipe = _make_pipeline(speaker_provider=lambda uid: "Jack")
    assert pipe._resolve_speaker("local") == "Jack"


def test_injected_provider_receives_correct_user_id():
    """確認 provider 拿到的是原始 user_id，而不是別的值。"""
    received = []
    pipe = _make_pipeline(speaker_provider=lambda uid: received.append(uid) or "Jack")
    pipe._resolve_speaker("local")
    assert received == ["local"]

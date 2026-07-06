"""
tests/test_satellite_identity_map.py

TDD：非 Discord 來源（衛星/本機）身分映射（DiscordVoiceEngine._resolve_speaker_name）。
先紅後綠。

背景（project_identity_unification）：衛星/本機 pipeline 帶字串 user_id（"satellite"
/"local"），需映射到既有講者身分（如「狗與露」）＝記憶延續。Discord 路徑（int
user_id）必須完全不受影響。

⚠️ runbook S4 原只在 discord_voice_engine.py:879（wake stream 早偵測）補映射，但衛星
音訊主路徑走 _flush_audio_to_stt（1196），只補 879 記憶仍會斷。故抽共用 helper 讓三處
（879/1071/1196）都吃映射，本測試守此不回歸。
"""
from __future__ import annotations

from unittest.mock import MagicMock

from discord_voice_engine import DiscordVoiceEngine


def _fake_engine(guilds=None):
    """造只夠跑 _resolve_speaker_name 的 fake self（不建整顆 engine）。"""
    fake = MagicMock()
    fake.bot.guilds = guilds if guilds is not None else []
    return fake


# ── 非 Discord 來源 + env 設定 → 映射到既有講者 ──────────────────────────────

def test_satellite_maps_to_owner_speaker_when_env_set(monkeypatch):
    monkeypatch.setenv("MARVIN_SATELLITE_SPEAKER", "狗與露")
    fake = _fake_engine(guilds=[])
    assert DiscordVoiceEngine._resolve_speaker_name(fake, "satellite") == "狗與露"


def test_local_maps_to_owner_speaker_when_env_set(monkeypatch):
    monkeypatch.setenv("MARVIN_LOCAL_SPEAKER", "狗與露")
    fake = _fake_engine(guilds=[])
    assert DiscordVoiceEngine._resolve_speaker_name(fake, "local") == "狗與露"


# ── env 不設 → 維持 User_xxx 舊行為（不亂認人）────────────────────────────────

def test_satellite_falls_back_to_user_id_when_env_unset(monkeypatch):
    monkeypatch.delenv("MARVIN_SATELLITE_SPEAKER", raising=False)
    fake = _fake_engine(guilds=[])
    assert DiscordVoiceEngine._resolve_speaker_name(fake, "satellite") == "User_satellite"


# ── Discord member 正常解析：不受身分映射影響 ────────────────────────────────

def test_discord_member_nick_resolves_normally(monkeypatch):
    # env 即使有設，Discord int user_id 找得到 member 時走 member 名，不碰映射
    monkeypatch.setenv("MARVIN_SATELLITE_SPEAKER", "狗與露")
    member = MagicMock()
    member.nick = "阿狗"
    member.display_name = "DogUser"
    guild = MagicMock()
    guild.get_member.return_value = member
    fake = _fake_engine(guilds=[guild])
    assert DiscordVoiceEngine._resolve_speaker_name(fake, 123456789) == "阿狗"


def test_discord_member_display_name_when_no_nick(monkeypatch):
    member = MagicMock()
    member.nick = None
    member.display_name = "DogUser"
    guild = MagicMock()
    guild.get_member.return_value = member
    fake = _fake_engine(guilds=[guild])
    assert DiscordVoiceEngine._resolve_speaker_name(fake, 123456789) == "DogUser"


def test_discord_int_user_not_in_cache_falls_back_to_user_id(monkeypatch):
    # int user_id 找不到 member 且非映射 key → User_<id>（舊行為，映射 key 不誤中）
    monkeypatch.setenv("MARVIN_SATELLITE_SPEAKER", "狗與露")
    guild = MagicMock()
    guild.get_member.return_value = None
    fake = _fake_engine(guilds=[guild])
    assert DiscordVoiceEngine._resolve_speaker_name(fake, 123456789) == "User_123456789"

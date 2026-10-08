"""TDD — DJ 口白播出判定與歸屬（第 1 刀 skip 訊號）。

_maybe_play_dj_interjection 現在回 None/"full"/"short"（而不是純 fire-and-forget），
_attach_narration/_play_and_attach_narration/_splice_and_attach 把「這段口白
確定播出」歸屬到它引介的歌（info 或 self._current_narration），供之後配對
skip 訊號。不驗算 select_mode / dj_topic_selector 等既有邏輯，只驗證這幾個
新函式本身的分支。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import dj_narration_log


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/short_dj.opus")
    bot.router = MagicMock()
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.conv_buffer.get_history = MagicMock(return_value=[])
    bot.music_memory = MagicMock()

    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    return cog


def _make_vc(*, play_dj_result=True, play_tts_result=True, intimate=False):
    vc = MagicMock()
    vc.play_tts = AsyncMock(return_value=play_tts_result)
    vc.play_dj_on_tts_layer = AsyncMock(return_value=play_dj_result)
    vc._intimate_mode = intimate
    vc.get_online_members = MagicMock(return_value=[])
    return vc


# ── _maybe_play_dj_interjection 回傳值 ───────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_none_when_dj_is_none():
    cog = _make_cog()
    assert await cog._maybe_play_dj_interjection(None) is None


@pytest.mark.asyncio
async def test_returns_none_when_text_empty():
    cog = _make_cog()
    assert await cog._maybe_play_dj_interjection({"text": ""}) is None


@pytest.mark.asyncio
async def test_returns_none_when_vc_is_none():
    cog = _make_cog()
    cog._vc = MagicMock(return_value=None)
    assert await cog._maybe_play_dj_interjection({"text": "口白"}) is None


@pytest.mark.asyncio
async def test_returns_none_when_intimate_mode():
    cog = _make_cog()
    cog._vc = MagicMock(return_value=_make_vc(intimate=True))
    assert await cog._maybe_play_dj_interjection({"text": "口白"}) is None


@pytest.mark.asyncio
async def test_returns_none_when_hot_and_no_short_text():
    cog = _make_cog()
    cog._dj_channel_is_hot = MagicMock(return_value=True)
    cog._vc = MagicMock(return_value=_make_vc())
    result = await cog._maybe_play_dj_interjection({"text": "口白", "short_text": ""})
    assert result is None


@pytest.mark.asyncio
async def test_returns_full_when_audio_path_plays_true(tmp_path):
    cog = _make_cog()
    vc = _make_vc(play_dj_result=True)
    cog._vc = MagicMock(return_value=vc)
    audio = tmp_path / "dj.opus"
    audio.write_bytes(b"x")
    result = await cog._maybe_play_dj_interjection({"text": "口白", "audio_path": str(audio)})
    assert result == "full"


@pytest.mark.asyncio
async def test_returns_none_when_audio_path_plays_false(tmp_path):
    cog = _make_cog()
    vc = _make_vc(play_dj_result=False)
    cog._vc = MagicMock(return_value=vc)
    audio = tmp_path / "dj.opus"
    audio.write_bytes(b"x")
    result = await cog._maybe_play_dj_interjection({"text": "口白", "audio_path": str(audio)})
    assert result is None


@pytest.mark.asyncio
async def test_returns_short_when_hot_and_short_text_plays(tmp_path):
    cog = _make_cog()
    cog._dj_channel_is_hot = MagicMock(return_value=True)
    short_audio = tmp_path / "short.opus"
    short_audio.write_bytes(b"x")
    cog.bot.tts_engine.generate_audio = AsyncMock(return_value=str(short_audio))
    vc = _make_vc(play_dj_result=True)
    cog._vc = MagicMock(return_value=vc)
    result = await cog._maybe_play_dj_interjection(
        {"text": "原口白", "audio_path": "/tmp/orig.opus", "short_text": "下一首，夜曲"}
    )
    assert result == "short"


@pytest.mark.asyncio
async def test_returns_none_when_no_audio_path_and_play_tts_false():
    cog = _make_cog()
    vc = _make_vc(play_tts_result=False)
    cog._vc = MagicMock(return_value=vc)
    result = await cog._maybe_play_dj_interjection({"text": "口白"})
    assert result is None


@pytest.mark.asyncio
async def test_returns_full_when_no_audio_path_and_play_tts_true():
    cog = _make_cog()
    vc = _make_vc(play_tts_result=True)
    cog._vc = MagicMock(return_value=vc)
    result = await cog._maybe_play_dj_interjection({"text": "口白"})
    assert result == "full"


# ── _attach_narration ────────────────────────────────────────────────────────

def test_attach_narration_noop_when_dj_none():
    cog = _make_cog()
    info = {}
    cog._attach_narration(info, None, "full")
    assert info == {}


def test_attach_narration_noop_when_no_narration_id():
    cog = _make_cog()
    info = {}
    cog._attach_narration(info, {"mode": "life"}, "full")
    assert info == {}


def test_attach_narration_stores_on_info_when_not_started(monkeypatch):
    cog = _make_cog()
    aired = []
    monkeypatch.setattr(dj_narration_log, "log_dj_narration", lambda rec: aired.append(rec))
    info = {}
    cog._attach_narration(info, {"narration_id": "nid1", "mode": "life"}, "full")
    assert info["_narration_id"] == "nid1"
    assert info["_narration_mode"] == "life"
    assert len(aired) == 1
    assert aired[0] == {"type": "aired", "narration_id": "nid1", "kind": "full"}


def test_attach_narration_short_kind_forces_short_mode(monkeypatch):
    cog = _make_cog()
    monkeypatch.setattr(dj_narration_log, "log_dj_narration", lambda rec: None)
    info = {}
    cog._attach_narration(info, {"narration_id": "nid1", "mode": "life"}, "short")
    assert info["_narration_mode"] == "short"


def test_attach_narration_updates_current_narration_when_already_started(monkeypatch):
    cog = _make_cog()
    plays = []
    monkeypatch.setattr(dj_narration_log, "log_dj_narration", lambda rec: None)
    monkeypatch.setattr(dj_narration_log, "log_song_play", lambda rec: plays.append(rec))
    info = {"title": "夜曲"}
    cog._current_play_info = info
    cog._current_play_id = "play1"
    cog._current_narration = (None, None)

    cog._attach_narration(info, {"narration_id": "nid1", "mode": "life"}, "full")

    assert "_narration_id" not in info
    assert cog._current_narration == ("nid1", "life")
    assert len(plays) == 1
    assert plays[0] == {
        "type": "narration_attach", "play_id": "play1", "narration_id": "nid1", "mode": "life",
    }


# ── _play_and_attach_narration ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_play_and_attach_narration_skips_attach_when_not_played():
    cog = _make_cog()
    cog._maybe_play_dj_interjection = AsyncMock(return_value=None)
    cog._attach_narration = MagicMock()
    info = {}
    await cog._play_and_attach_narration(info, {"narration_id": "nid1"})
    cog._attach_narration.assert_not_called()


@pytest.mark.asyncio
async def test_play_and_attach_narration_attaches_when_played():
    cog = _make_cog()
    cog._maybe_play_dj_interjection = AsyncMock(return_value="full")
    cog._attach_narration = MagicMock()
    info = {}
    dj = {"narration_id": "nid1"}
    await cog._play_and_attach_narration(info, dj)
    cog._attach_narration.assert_called_once_with(info, dj, "full")


# ── _splice_and_attach ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_splice_and_attach_attaches_when_file_exists(tmp_path):
    cog = _make_cog()
    spliced = tmp_path / "spliced.opus"
    spliced.write_bytes(b"x")
    cog._splice_owner_voice_clip = AsyncMock(return_value=str(spliced))
    cog._attach_narration = MagicMock()
    info = {}
    dj = {"narration_id": "nid1"}
    out = await cog._splice_and_attach("/tmp/dj.opus", info, dj)
    assert out == str(spliced)
    cog._attach_narration.assert_called_once_with(info, dj, "full")


@pytest.mark.asyncio
async def test_splice_and_attach_no_attach_when_none():
    cog = _make_cog()
    cog._splice_owner_voice_clip = AsyncMock(return_value=None)
    cog._attach_narration = MagicMock()
    info = {}
    dj = {"narration_id": "nid1"}
    out = await cog._splice_and_attach("/tmp/dj.opus", info, dj)
    assert out is None
    cog._attach_narration.assert_not_called()

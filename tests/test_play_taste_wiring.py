"""MusicAudioMetaMixin._record_play 接線測試：record_play 必呼叫、強化口味條件正確、失敗不 raise。"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

from cogs.music_cog_audio_meta import MusicAudioMetaMixin

MUSIC_COG = Path(__file__).resolve().parent.parent / "cogs" / "music_cog.py"


def _fake_self(*, has_player=True):
    fake = MagicMock()
    fake.bot.router.memory.has_player.return_value = has_player
    fake.bot.router.memory.get_player_memory.return_value = {
        "taste": {"盧廣仲": {"score": 1.5, "last_update": 0.0}},
        "taboos": [],
    }
    return fake


def test_record_play_human_request_reinforces_existing_taste():
    fake = _fake_self()
    info = {"title": "盧廣仲【淵明】", "requested_by": "狗與露"}
    MusicAudioMetaMixin._record_play(fake, info, "狗與露")
    fake.bot.music_memory.record_play.assert_called_once_with(info, "狗與露")
    fake.bot.router.memory.record_taste_signal.assert_called_once_with(
        "狗與露", "盧廣仲", 0.5, reason="song_request"
    )


def test_record_play_pseudo_player_skips_reinforce_but_still_records_play():
    fake = _fake_self()
    info = {"title": "盧廣仲【淵明】"}
    MusicAudioMetaMixin._record_play(fake, info, "Marvin推薦（為狗與露）")
    fake.bot.music_memory.record_play.assert_called_once_with(info, "Marvin推薦（為狗與露）")
    fake.bot.router.memory.record_taste_signal.assert_not_called()


def test_record_play_no_existing_player_skips_reinforce():
    fake = _fake_self(has_player=False)
    info = {"title": "盧廣仲【淵明】"}
    MusicAudioMetaMixin._record_play(fake, info, "狗與露")
    fake.bot.router.memory.record_taste_signal.assert_not_called()


def test_record_play_reinforce_exception_does_not_raise():
    fake = _fake_self()
    fake.bot.router.memory.record_taste_signal.side_effect = RuntimeError("boom")
    info = {"title": "盧廣仲【淵明】"}
    MusicAudioMetaMixin._record_play(fake, info, "狗與露")  # 不該 raise


def test_music_cog_wired_to_record_play_helper():
    src = MUSIC_COG.read_text(encoding="utf-8")
    ast.parse(src)
    assert "self._record_play(info, info.get('_record_as') or requested_by)" in src
    assert "self.bot.music_memory.record_play(" not in src

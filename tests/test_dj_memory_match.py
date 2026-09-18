"""TDD: DJ 口白「記憶對歌」mode（memory_match）。

背景：`dj_social_affinity.find_song_social_affinity` 挖出的「這首歌跟誰的記憶
有關」證據，過去只被當成 ctx 裡一行「喜好線索」丟給 LLM，常被 life 話題蓋過。
這裡新增：在場者「親口說過喜歡」的歌手/歌 ↔ 這首歌的強匹配時，直接以此為
主題開場點名講出來（mode="memory_match"），優先序排在最前面（在 select_mode
的 life/interest/... 挑選之前攔截）。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dj_narration_orchestrator import select_narration_mode
from dj_social_affinity import find_spoken_taste_match
from dj_topic_selector import TopicCooldownStore


# ── B1. find_spoken_taste_match ─────────────────────────────────────────────

class _FakeSuki:
    def __init__(self, players: dict):
        self._players = players
        self.get_player_memory_calls: list[str] = []

    def has_player(self, name: str) -> bool:
        return name in self._players

    def get_player_memory(self, name: str) -> dict:
        self.get_player_memory_calls.append(name)
        return self._players[name]


def test_artist_exact_match():
    suki = _FakeSuki({"陳進文": {"likes": ["露營", "伍佰"], "taboos": []}})
    result = find_spoken_taste_match(suki, "痛哭的人", "伍佰", ["陳進文"])
    assert result == "陳進文 說過喜歡伍佰"


def test_like_appears_in_title_match():
    suki = _FakeSuki({"陳進文": {"likes": ["痛哭的人"], "taboos": []}})
    result = find_spoken_taste_match(suki, "痛哭的人 (Live)", "伍佰", ["陳進文"])
    assert result == "陳進文 說過喜歡痛哭的人"


def test_clean_artist_contained_in_like_match():
    suki = _FakeSuki({"陳進文": {"likes": ["伍佰的歌"], "taboos": []}})
    result = find_spoken_taste_match(suki, "任意歌名", "伍佰", ["陳進文"])
    assert result == "陳進文 說過喜歡伍佰的歌"


def test_like_too_short_no_match():
    suki = _FakeSuki({"陳進文": {"likes": ["伍"], "taboos": []}})
    result = find_spoken_taste_match(suki, "任意歌名", "伍佰", ["陳進文"])
    assert result is None


def test_like_in_taboos_no_match():
    suki = _FakeSuki({"陳進文": {"likes": ["伍佰"], "taboos": ["伍佰"]}})
    result = find_spoken_taste_match(suki, "任意歌名", "伍佰", ["陳進文"])
    assert result is None


def test_has_player_false_skips_without_calling_get_player_memory():
    suki = _FakeSuki({})
    result = find_spoken_taste_match(suki, "任意歌名", "伍佰", ["陳進文"])
    assert result is None
    assert suki.get_player_memory_calls == []


def test_marvin_prefixed_name_skipped():
    suki = _FakeSuki({"Marvin推薦（為Alice）": {"likes": ["伍佰"], "taboos": []}})
    result = find_spoken_taste_match(suki, "任意歌名", "伍佰", ["Marvin推薦（為Alice）"])
    assert result is None
    assert suki.get_player_memory_calls == []


def test_order_returns_first_matching_person():
    suki = _FakeSuki({
        "陳進文": {"likes": ["伍佰"], "taboos": []},
        "小美": {"likes": ["伍佰"], "taboos": []},
    })
    result = find_spoken_taste_match(suki, "任意歌名", "伍佰", ["陳進文", "小美"])
    assert result == "陳進文 說過喜歡伍佰"


def test_no_match_returns_none():
    suki = _FakeSuki({"陳進文": {"likes": ["露營"], "taboos": []}})
    result = find_spoken_taste_match(suki, "任意歌名", "伍佰", ["陳進文"])
    assert result is None


def test_suki_none_returns_none():
    assert find_spoken_taste_match(None, "任意歌名", "伍佰", ["陳進文"]) is None


def test_empty_people_returns_none():
    suki = _FakeSuki({"陳進文": {"likes": ["伍佰"], "taboos": []}})
    assert find_spoken_taste_match(suki, "任意歌名", "伍佰", []) is None


def test_likes_not_a_list_does_not_crash():
    suki = _FakeSuki({"陳進文": {"likes": "not-a-list", "taboos": None}})
    assert find_spoken_taste_match(suki, "任意歌名", "伍佰", ["陳進文"]) is None


# ── B2. select_narration_mode(memory_evidence=...) ──────────────────────────

def _store(tmp_path, now=None):
    kwargs = {"path": str(tmp_path / "cd.json")}
    if now is not None:
        kwargs["now"] = now
    return TopicCooldownStore(**kwargs)


def test_memory_evidence_wins_and_does_not_consume_life_cooldown(tmp_path):
    store = _store(tmp_path)
    life = ["今天出去露營了"]
    topic, mode = select_narration_mode(
        life=life,
        interests=[],
        topic_store=store,
        memory_evidence="陳進文 說過喜歡伍佰",
    )
    assert (topic, mode) == ("陳進文 說過喜歡伍佰", "memory_match")
    assert store.is_cool("今天出去露營了") is True


def test_memory_evidence_in_cooldown_falls_back_to_normal_flow(tmp_path):
    store = _store(tmp_path)
    life = ["今天出去露營了"]
    ev = "陳進文 說過喜歡伍佰"
    store.mark_used(ev)
    topic, mode = select_narration_mode(
        life=life,
        interests=[],
        topic_store=store,
        memory_evidence=ev,
    )
    assert (topic, mode) == ("今天出去露營了", "life")


def test_no_memory_evidence_matches_default_behavior(tmp_path):
    life = ["今天出去露營了"]
    store_a = TopicCooldownStore(path=str(tmp_path / "cd_a.json"))
    result_without = select_narration_mode(life=life, interests=[], topic_store=store_a)

    store_b = TopicCooldownStore(path=str(tmp_path / "cd_b.json"))
    result_with_empty = select_narration_mode(
        life=life, interests=[], topic_store=store_b, memory_evidence="",
    )
    assert result_without == result_with_empty == ("今天出去露營了", "life")


# ── B3. 接進 _fetch_dj_interjection_raw ─────────────────────────────────────

def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/dj_audio.opus")
    bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)
    bot.router = MagicMock()
    bot.router.generate_dynamic_system_msg = AsyncMock(
        return_value="唉...又是伍佰，陳進文的老朋友"
    )
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    bot.engine.post_summon_callback = None

    bot.music_memory = MagicMock()
    bot.music_memory._key = MagicMock(return_value="song_key_xyz")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="深夜")

    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    cog._enable_dj_news_fetch = False
    return cog


def _info(title="伍佰 - 痛哭的人", artist="伍佰", requester="陳進文"):
    return {
        "title": title,
        "uploader": artist,
        "requested_by": requester,
        "url": "https://example/x",
    }


@pytest.mark.asyncio
async def test_memory_match_context_includes_spoken_evidence(tmp_path):
    cog = _make_cog()
    cog._dj_topic_cooldown_store = TopicCooldownStore(path=str(tmp_path / "cd.json"))
    cog.bot.router.memory = _FakeSuki({"陳進文": {"likes": ["伍佰"], "taboos": []}})
    with patch.object(cog, "_dj_clean_name", return_value=("痛哭的人", "伍佰")):
        await cog._fetch_dj_interjection_raw(_info())

    call = cog.bot.router.generate_dynamic_system_msg.call_args
    assert call is not None
    ctx = call.kwargs.get("context", "")
    assert "記憶證據" in ctx
    assert "陳進文 說過喜歡伍佰" in ctx


@pytest.mark.asyncio
async def test_no_spoken_match_no_memory_evidence_in_context(tmp_path):
    cog = _make_cog()
    cog._dj_topic_cooldown_store = TopicCooldownStore(path=str(tmp_path / "cd.json"))
    cog.bot.router.memory = _FakeSuki({"陳進文": {"likes": ["露營"], "taboos": []}})
    with patch.object(cog, "_dj_clean_name", return_value=("痛哭的人", "伍佰")):
        await cog._fetch_dj_interjection_raw(_info())

    call = cog.bot.router.generate_dynamic_system_msg.call_args
    assert call is not None
    ctx = call.kwargs.get("context", "")
    assert "記憶證據" not in ctx

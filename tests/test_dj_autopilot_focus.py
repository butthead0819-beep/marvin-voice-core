"""autopilot 口白依熟悉度分流（10/4 使用者定）。

- 懷舊位不講話：聽的人知道是什麼歌，「幾週前播過」沒意義，直接接歌不打斷節奏。
- 新歌/少播（全伺服器 ≤2 次）：多講歌曲本身。
- 熟歌：串話題口白（不走導聆）。
- 只限 autopilot（requester 以 Marvin 開頭）；真人點歌行為不變。
"""
from __future__ import annotations

import random
from unittest.mock import AsyncMock, MagicMock

import pytest

import dj_narration_orchestrator
from dj_narration_orchestrator import RARE_MAX_PLAYS, autopilot_narration_focus, select_narration_mode
from dj_topic_selector import TopicCooldownStore
from tests.test_dj_story_context import _ctx_str, _info, _make_cog

AUTO = "Marvin推薦（為大肚）"


# ── autopilot_narration_focus（純函式）────────────────────────────────────────

def test_focus_human_request_is_unchanged():
    assert autopilot_narration_focus({"requested_by": "大肚", "_arc_role": "nostalgia",
                                      "_server_plays": 0}) == ""


def test_focus_nostalgia_is_silent():
    assert autopilot_narration_focus({"requested_by": AUTO, "_arc_role": "nostalgia",
                                      "_server_plays": 30}) == "silent"


@pytest.mark.parametrize("plays,expect", [(0, "song"), (RARE_MAX_PLAYS, "song"),
                                          (RARE_MAX_PLAYS + 1, "topic")])
def test_focus_by_server_plays(plays, expect):
    assert RARE_MAX_PLAYS == 2
    assert autopilot_narration_focus({"requested_by": AUTO, "_arc_role": "discovery",
                                      "_server_plays": plays}) == expect


def test_focus_missing_server_plays_is_unchanged():
    assert autopilot_narration_focus({"requested_by": AUTO}) == ""


# ── select_narration_mode(focus=...) ─────────────────────────────────────────

def _spy_select_mode(monkeypatch):
    calls = []

    def _fake(*a, **kw):
        calls.append(kw)
        return None, "atmosphere"
    monkeypatch.setattr(dj_narration_orchestrator, "select_mode", _fake)
    return calls


def _store(tmp_path):
    return TopicCooldownStore(path=str(tmp_path / "cd.json"))


def test_song_focus_with_guide_still_goes_song_facet(tmp_path, monkeypatch):
    # 10/4 改：導聆變成 song 素材池的一個 facet（pick_song_facet 抽），mode 一律 "song"
    calls = _spy_select_mode(monkeypatch)
    out = select_narration_mode(life=[], interests=[], topic_store=_store(tmp_path),
                                has_guide=True, focus="song", autopilot_reason="理由")
    assert out == (None, "song")
    assert calls == []


def test_song_focus_without_guide_goes_song_without_gacha(tmp_path, monkeypatch):
    calls = _spy_select_mode(monkeypatch)
    out = select_narration_mode(life=[], interests=[], topic_store=_store(tmp_path),
                                has_guide=False, focus="song", autopilot_reason="理由")
    assert out == (None, "song")
    assert calls == []


def test_topic_focus_excludes_guide_from_gacha(tmp_path, monkeypatch):
    calls = _spy_select_mode(monkeypatch)
    select_narration_mode(life=[], interests=[], topic_store=_store(tmp_path),
                          has_guide=True, focus="topic")
    assert calls[0]["has_guide"] is False


def test_no_focus_passes_has_guide_through(tmp_path, monkeypatch):
    calls = _spy_select_mode(monkeypatch)
    select_narration_mode(life=[], interests=[], topic_store=_store(tmp_path), has_guide=True)
    assert calls[0]["has_guide"] is True


def test_memory_evidence_still_wins_over_song_focus(tmp_path, monkeypatch):
    calls = _spy_select_mode(monkeypatch)
    out = select_narration_mode(life=[], interests=[], topic_store=_store(tmp_path),
                                memory_evidence="大肚說過超愛這個歌手", focus="song")
    assert out == ("大肚說過超愛這個歌手", "memory_match")
    assert calls == []


# ── _fetch_dj_interjection_raw ───────────────────────────────────────────────

def _auto_info(**kw):
    info = _info(title="周杰倫 - 夜曲", requester=AUTO)
    info.update(kw)
    return info


@pytest.mark.asyncio
async def test_nostalgia_autopilot_song_has_no_narration(tmp_path):
    cog = _make_cog(tmp_path=tmp_path)
    cog.stream_history = []
    result = await cog._fetch_dj_interjection_raw(_auto_info(_arc_role="nostalgia", _server_plays=20))
    assert result is None
    cog.bot.router.generate_dynamic_system_msg.assert_not_called()
    cog.bot.tts_engine.generate_audio.assert_not_called()


@pytest.mark.asyncio
async def test_human_request_with_nostalgia_role_still_narrates(tmp_path):
    cog = _make_cog(tmp_path=tmp_path)
    cog.stream_history = []
    info = _info(title="周杰倫 - 夜曲", requester="大肚")
    info["_arc_role"] = "nostalgia"
    result = await cog._fetch_dj_interjection_raw(info)
    assert result is not None


@pytest.mark.asyncio
async def test_rare_autopilot_song_talks_about_song_not_topics(tmp_path):
    from dj_life_context import LifeCore
    cog = _make_cog(tmp_path=tmp_path)
    cog.stream_history = []
    cog._life_cores_async = AsyncMock(return_value=[LifeCore(text="大肚今天去爬山")])
    cog._present_interests = MagicMock(return_value=["露營"])
    cog._dj_song_material = AsyncMock(return_value=(
        {"artist": "周杰倫", "title": "夜曲", "album": "十一月的蕭邦", "year": 2005}, None))
    bank = cog._dj_heat_bank()
    bank.take = MagicMock(return_value=[])

    result = await cog._fetch_dj_interjection_raw(_auto_info(_arc_role="discovery", _server_plays=1))

    assert result is not None
    ctx = _ctx_str(cog)
    assert "介紹這首歌本身" in ctx
    assert "十一月的蕭邦" in ctx
    assert "爬山" not in ctx and "露營" not in ctx
    # 10/4 改：熱聊降溫接回話題對所有歌都優先，少播歌也會 take（不再跳過）
    bank.take.assert_called_once()


# ── 講歌模式去套路（10/4 午實測：6 首都是「照口味挖出、比較少聽、某年專輯、這首給你」）──

@pytest.mark.asyncio
async def test_song_mode_related_facet_is_pick_reason_and_bans_meta_words(tmp_path, monkeypatch):
    # 10/4 改：「選這首的理由」變成 song 素材池的 related facet（不再無條件剔除）。
    # 強制 random.choice 取最後一個候選（related），驗證理由以歌本身素材的形式進 ctx。
    monkeypatch.setattr(random, "choice", lambda seq: seq[-1])
    cog = _make_cog(tmp_path=tmp_path)
    cog.stream_history = []
    cog._dj_song_material = AsyncMock(return_value=(
        {"artist": "周杰倫", "title": "夜曲", "album": "十一月的蕭邦", "year": 2005}, None))
    info = _auto_info(_arc_role="discovery", _server_plays=0,
                      _explanation="YouTube Music 常把這首和你們聽過的《晴天》放在同一份歌單")

    await cog._fetch_dj_interjection_raw(info)

    ctx = _ctx_str(cog)
    assert "選這首的理由：YouTube Music 常把這首和你們聽過的《晴天》" in ctx
    assert "十一月的蕭邦" not in ctx
    assert "挖出" in ctx and "比較少聽" in ctx  # 出現在禁止詞清單裡


@pytest.mark.asyncio
async def test_song_mode_without_facts_uses_local_template_not_llm(tmp_path):
    cog = _make_cog(tmp_path=tmp_path)
    cog.stream_history = []
    cog._dj_song_material = AsyncMock(return_value=(None, None))

    result = await cog._fetch_dj_interjection_raw(_auto_info(_arc_role="discovery", _server_plays=0))

    assert result is not None
    cog.bot.router.generate_dynamic_system_msg.assert_not_called()

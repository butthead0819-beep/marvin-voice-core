"""TDD: 推薦掛名規則（2026-07-02 使用者訂）。

問題：Marvin 自動推薦掛「為A」的歌不是 A 點過的 → 使用者混淆
（discovery 新歌 A 根本沒聽過、themed 主題歌單也不是 A 的歷史）。

規則：
  1. 掛名「為A」 ⟹ 這首歌必須出自 A 的點播歷史（requesters 含 A）
  2. 跟個人點播歷史無關 ⟹ 掛「點給大家」，不指名
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from music_memory import MusicMemory, recommend_attribution, GROUP_ATTRIBUTION


def _info(vid="dQw4w9WgXcQ", title="晴天"):
    return {"title": title, "uploader": "周杰倫",
            "webpage_url": f"https://www.youtube.com/watch?v={vid}"}


# ── is_requester ─────────────────────────────────────────────────────────────

def test_is_requester_true_when_user_played_it(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    info = _info()
    mm.record_play(info, "阿明")
    assert mm.is_requester(info, "阿明") is True


def test_is_requester_false_for_other_user(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    info = _info()
    mm.record_play(info, "阿明")
    assert mm.is_requester(info, "狗與露") is False


def test_is_requester_false_for_unknown_song(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    assert mm.is_requester(_info(), "阿明") is False


def test_is_requester_marvin_pseudo_requester_does_not_count(tmp_path):
    """歌只被「Marvin推薦（為阿明）」播過 ≠ 阿明本人點過。"""
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    info = _info()
    mm.record_play(info, "Marvin推薦（為阿明）")
    assert mm.is_requester(info, "阿明") is False


# ── recommend_attribution ────────────────────────────────────────────────────

def test_attribution_personal_when_in_history(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    info = _info()
    mm.record_play(info, "阿明")
    assert recommend_attribution(mm, info, "阿明") == "Marvin推薦（為阿明）"


def test_attribution_group_when_not_in_history(tmp_path):
    """discovery 新歌 / 別人的歌 → 點給大家，不指名。"""
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    assert recommend_attribution(mm, _info(), "阿明") == GROUP_ATTRIBUTION


def test_attribution_group_when_mm_or_spotlight_missing(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    assert recommend_attribution(None, _info(), "阿明") == GROUP_ATTRIBUTION
    assert recommend_attribution(mm, _info(), "") == GROUP_ATTRIBUTION


def test_group_attribution_still_excluded_from_taste():
    """「點給大家」變體必須同樣被真人計數排除（防回音室）。"""
    from taste_fingerprint import _is_human
    assert _is_human(GROUP_ATTRIBUTION) is False
    assert _is_human("Marvin推薦（為阿明）") is False


# ── blurb 文案一致性 ─────────────────────────────────────────────────────────

def _cand(lane, target=None):
    c = MagicMock()
    c.lane = lane
    c.target_member = target
    return c


@pytest.mark.parametrize("lane", ["long_tail", "discovery", "exploit"])
def test_blurb_group_mode_does_not_name_anyone(lane):
    from cogs.music_cog import MusicCog
    blurb = MusicCog._recommend_blurb(
        MagicMock(), _cand(lane, target="阿明"), "晴天",
        spotlight="阿明", personal=False)
    assert "阿明" not in blurb
    assert "晴天" in blurb


def test_blurb_personal_mode_names_target():
    from cogs.music_cog import MusicCog
    blurb = MusicCog._recommend_blurb(
        MagicMock(), _cand("discovery", target="阿明"), "晴天",
        spotlight="阿明", personal=True)
    assert "阿明" in blurb

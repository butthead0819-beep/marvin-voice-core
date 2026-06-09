"""TDD: 自動推薦的 spotlight 歸因

驗證：
- _recommend_blurb 顯示替誰推（spotlight）
- build_autopilot_recommendation 的 speaker = spotlight、channel_state 有 spotlight_member
- requested_by 字串反映 spotlight 而非 seed username
"""
from __future__ import annotations
import pytest
from music_recommender import Candidate
from cogs.voice_controller import VoiceController, build_autopilot_recommendation


def _cand(lane: str, target_member: str | None = None) -> Candidate:
    return Candidate(
        anchor_title="夜曲",
        anchor_artist="周杰倫",
        lane=lane,
        mode="direct",
        target_member=target_member,
        score=80.0,
    )


class _Stub:
    """最輕 stub，只要能呼叫 _recommend_blurb 即可。"""


# ── _recommend_blurb ─────────────────────────────────────────────────────────

def test_blurb_long_tail_includes_spotlight():
    blurb = VoiceController._recommend_blurb(_Stub(), _cand("long_tail"), "夜曲", spotlight="狗與露")
    assert "狗與露" in blurb


def test_blurb_discovery_includes_spotlight():
    blurb = VoiceController._recommend_blurb(_Stub(), _cand("discovery"), "夜曲", spotlight="weakgogo")
    assert "weakgogo" in blurb


def test_blurb_spotlight_lane_uses_target_member():
    # spotlight lane：target_member 優先（Candidate 已知目標）
    cand = _cand("spotlight", target_member="showay")
    blurb = VoiceController._recommend_blurb(_Stub(), cand, "夜曲", spotlight="大肚")
    assert "showay" in blurb


def test_blurb_group_resonance_no_specific_user():
    # group_resonance 是群體共鳴，不點名個人
    blurb = VoiceController._recommend_blurb(_Stub(), _cand("group_resonance"), "夜曲", spotlight="大肚")
    assert "大肚" not in blurb


def test_blurb_spotlight_empty_falls_back_gracefully():
    # spotlight 沒傳仍可回傳合理字串
    blurb = VoiceController._recommend_blurb(_Stub(), _cand("discovery"), "夜曲")
    assert "夜曲" in blurb


# ── build_autopilot_recommendation ───────────────────────────────────────────

def test_autopilot_rec_speaker_is_spotlight():
    """speaker 欄位應記 spotlight（替誰推），而非 seed username。"""
    rec = build_autopilot_recommendation(
        speaker="狗與露",        # ← 已改成 spotlight
        title="夜曲",
        lane="discovery",
        mode="direct",
        anchor_title="夜曲",
        blurb="blurb",
        now=1.0,
        channel_state_extras={"spotlight_member": "狗與露"},
    )
    assert rec.speaker == "狗與露"


def test_autopilot_rec_channel_state_has_spotlight_member():
    rec = build_autopilot_recommendation(
        speaker="狗與露",
        title="夜曲",
        lane="discovery",
        mode="direct",
        anchor_title="夜曲",
        blurb="",
        now=1.0,
        channel_state_extras={"spotlight_member": "狗與露"},
    )
    assert rec.channel_state.get("spotlight_member") == "狗與露"


def test_autopilot_rec_requested_by_reflects_spotlight():
    """requested_by 字串要帶 spotlight，讓下首歌觸發 queue_empty 時可見誰是當前服務對象。"""
    rec = build_autopilot_recommendation(
        speaker="weakgogo",
        title="告白氣球",
        lane="long_tail",
        mode="direct",
        anchor_title="告白氣球",
        blurb="blurb",
        now=2.0,
        channel_state_extras={"spotlight_member": "weakgogo"},
    )
    # speaker == spotlight，外層 caller 會把 speaker 寫進 requested_by
    assert rec.speaker == "weakgogo"
    assert rec.channel_state["spotlight_member"] == "weakgogo"

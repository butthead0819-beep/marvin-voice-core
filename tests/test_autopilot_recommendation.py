"""TDD: build_autopilot_recommendation — 把佇列空時的 autopilot 推薦包成
Recommendation 寫進 records/agent_recommendations.jsonl（offline feedback log）。

純函式測試，不需實例化 VoiceController。

Bug 2026-05-25 contexts:
昨日 autopilot 推了 37 首歌（張雨生 6 次、慢冷 11 次），但這條 path 從沒寫進
agent_recommendations.jsonl，導致夜間 feedback 分析完全看不到——回饋黑洞。
"""
from __future__ import annotations

from cogs.voice_controller import build_autopilot_recommendation


def test_autopilot_recommendation_basic_fields():
    rec = build_autopilot_recommendation(
        speaker="大肚",
        title="張雨生 - 以為你都知道",
        lane="spotlight",
        mode="cover",
        anchor_title="以為你都知道",
        blurb="🎵 為大肚翻出的《以為你都知道》",
        now=123.0,
    )
    assert rec.agent == "music"
    assert rec.speaker == "大肚"
    assert rec.trigger == "queue_empty"
    assert rec.selected == "張雨生 - 以為你都知道"
    assert rec.ts == 123.0
    assert rec.feedback_window_s == 300
    assert rec.explanation_uttered == "🎵 為大肚翻出的《以為你都知道》"
    # reason_internal 帶 lane + mode + anchor 供分析端抽特徵
    assert "spotlight" in rec.reason_internal
    assert "cover" in rec.reason_internal
    assert "以為你都知道" in rec.reason_internal


def test_autopilot_recommendation_channel_state_records_lane_mode():
    rec = build_autopilot_recommendation(
        speaker="狗與露", title="周杰倫 - 晴天",
        lane="group_resonance", mode="direct",
        anchor_title="晴天", blurb="", now=1.0,
    )
    assert rec.channel_state["lane"] == "group_resonance"
    assert rec.channel_state["mode"] == "direct"


def test_autopilot_recommendation_empty_blurb_ok():
    """blurb 可空（autopilot 第 2、3 首不發訊息），不應炸。"""
    rec = build_autopilot_recommendation(
        speaker="X", title="某歌", lane="long_tail", mode="direct",
        anchor_title="某歌", blurb="", now=1.0,
    )
    assert rec.explanation_uttered == ""

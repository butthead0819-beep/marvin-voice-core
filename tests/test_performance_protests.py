"""表演聽感（抗議偵測）— daily review 的確定性檢查（不靠 LLM 標註）。

2026-06-12：主動表演（marvin_sing/manzai/standup/news/imitate/joke）連發上線後
只有觸發次數與 ±90s LLM reaction，沒有「被打斷感」的直接訊號。補一條：
表演 fire 後 60s 內逐字稿掃抗議關鍵字（閉嘴/吵死/不要唱…），逐日落在
topic_stats json 供趨勢追蹤。

純 core 函式（per design_disciplines_for_future_consumers）：
parse_stt_utterances(lines) / compute_performance_protests(fires, utterances)。
"""
from __future__ import annotations

from datetime import datetime

from scripts.analyze_daily_log import (
    compute_performance_protests,
    parse_stt_utterances,
)


# ── parse_stt_utterances ───────────────────────────────────────────────────

def test_parse_extracts_debounced_lines():
    lines = [
        "2026-06-05 23:56:50,382 - [showay] (Debounced) 我是馬文他爸",
        "2026-06-05 23:56:56,428 - [陳進文] (Debounced) 不錯喔",
    ]
    out = parse_stt_utterances(lines)

    assert len(out) == 2
    ts, speaker, text = out[0]
    assert speaker == "showay"
    assert text == "我是馬文他爸"
    assert ts == datetime(2026, 6, 5, 23, 56, 50).timestamp()


def test_parse_extracts_wake_lines():
    lines = [
        "2026-06-05 23:58:52,159 - [⚡喚醒] [showay] raw='馬文閉嘴' | Track=B | wake_intent=None",
    ]
    out = parse_stt_utterances(lines)

    assert len(out) == 1
    assert out[0][1] == "showay"
    assert out[0][2] == "馬文閉嘴"


def test_parse_skips_non_utterance_lines():
    lines = [
        "=== STT 切片 2026-06-05 12:00 ~ 2026-06-06 12:00 ===",
        "2026-06-05 23:55:23,175 - [BOT降臨] showay，你的抱怨沉重。",
        "2026-06-05 23:58:03,685 - [BOT→陳進文] (喚醒延遲=15.4s) 隨便你吧。",
        "not a log line",
    ]
    assert parse_stt_utterances(lines) == []


# ── compute_performance_protests ───────────────────────────────────────────

def _fire(ts: float, topic_id: str = "marvin_sing", title: str = "即興自彈自唱") -> dict:
    return {"timestamp": ts, "topic_id": topic_id, "title": title}


def test_protest_within_window_counted_with_sample():
    fires = [_fire(1000.0)]
    utts = [(1030.0, "showay", "好了好了閉嘴")]

    stats = compute_performance_protests(fires, utts)

    assert stats["total_performances"] == 1
    assert stats["protest_count"] == 1
    p = stats["protests"][0]
    assert p["topic_id"] == "marvin_sing"
    assert p["speaker"] == "showay"
    assert "閉嘴" in p["text"]


def test_utterance_outside_window_not_counted():
    fires = [_fire(1000.0)]
    utts = [(1061.0, "showay", "吵死了"), (999.0, "showay", "閉嘴")]

    stats = compute_performance_protests(fires, utts, window_s=60)

    assert stats["protest_count"] == 0


def test_non_performance_fires_ignored():
    fires = [_fire(1000.0, topic_id="topic_work_stress_release", title="聊天話題")]
    utts = [(1010.0, "showay", "閉嘴")]

    stats = compute_performance_protests(fires, utts)

    assert stats["total_performances"] == 0
    assert stats["protest_count"] == 0


def test_non_protest_utterances_ignored():
    fires = [_fire(1000.0)]
    utts = [(1010.0, "showay", "這首不錯聽"), (1020.0, "大肚", "再來一首")]

    stats = compute_performance_protests(fires, utts)

    assert stats["total_performances"] == 1
    assert stats["protest_count"] == 0


def test_per_topic_breakdown():
    fires = [
        _fire(1000.0, "marvin_sing"),
        _fire(2000.0, "marvin_manzai", "雙口漫才表演"),
        _fire(3000.0, "marvin_manzai", "雙口漫才表演"),
    ]
    utts = [(2030.0, "狗與露", "好吵喔"), (3010.0, "showay", "不要唱了")]

    stats = compute_performance_protests(fires, utts)

    assert stats["per_topic"]["marvin_sing"] == {"fires": 1, "protests": 0}
    assert stats["per_topic"]["marvin_manzai"] == {"fires": 2, "protests": 2}


def test_empty_fires_returns_zero():
    stats = compute_performance_protests([], [(1010.0, "showay", "閉嘴")])

    assert stats["total_performances"] == 0
    assert stats["protest_count"] == 0
    assert stats["protests"] == []

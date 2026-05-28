"""IntentGapRecord schema — agent_gaps.jsonl 的單筆紀錄格式。

設計重點：
- schema_version=1 從第一筆就有（design discipline）
- UNKNOWN intent_type 是 classifier failure 的合法狀態
- nearest_agent / nearest_distance / ack_text 在 UNKNOWN 或 dedup 時為 None
"""
import json

from intent_gap import IntentGapRecord


def _sample_record(**overrides):
    base = dict(
        utterance_id="u-1",
        ts=1234567890.0,
        speaker="alice",
        mode="normal",
        raw_query="播我點過的歌",
        cleaned_query="播我點過的歌",
        intent_type="replay_user_history",
        slots={"target_user": "self"},
        nearest_agent="music_v2",
        nearest_distance=0.45,
        ack_text="這個我還沒會，已經記下來。",
        acknowledged=True,
    )
    base.update(overrides)
    return IntentGapRecord(**base)


def test_intent_gap_record_defaults_schema_version_to_1():
    rec = _sample_record()
    assert rec.schema_version == 1


def test_intent_gap_record_to_jsonl_serializes_all_fields():
    rec = _sample_record()
    line = rec.to_jsonl()
    parsed = json.loads(line)

    assert parsed["schema_version"] == 1
    assert parsed["utterance_id"] == "u-1"
    assert parsed["speaker"] == "alice"
    assert parsed["mode"] == "normal"
    assert parsed["intent_type"] == "replay_user_history"
    assert parsed["slots"] == {"target_user": "self"}
    assert parsed["nearest_agent"] == "music_v2"
    assert parsed["nearest_distance"] == 0.45
    assert parsed["ack_text"] == "這個我還沒會，已經記下來。"
    assert parsed["acknowledged"] is True


def test_intent_gap_record_to_jsonl_preserves_unicode():
    """中文不能變 \\uXXXX escape，否則人類讀 jsonl 痛苦。"""
    rec = _sample_record()
    line = rec.to_jsonl()
    assert "播我點過的歌" in line
    assert "\\u" not in line


def test_intent_gap_record_to_jsonl_roundtrip():
    rec = _sample_record()
    parsed = json.loads(rec.to_jsonl())
    rec2 = IntentGapRecord.from_dict(parsed)
    assert rec2 == rec


def test_intent_gap_record_unknown_failure_case():
    """Classifier 掛掉 → intent_type=UNKNOWN, acknowledged=False, ack_text=None。"""
    rec = _sample_record(
        intent_type="UNKNOWN",
        slots={},
        nearest_agent=None,
        nearest_distance=None,
        ack_text=None,
        acknowledged=False,
    )
    parsed = json.loads(rec.to_jsonl())
    assert parsed["intent_type"] == "UNKNOWN"
    assert parsed["acknowledged"] is False
    assert parsed["ack_text"] is None
    assert parsed["nearest_agent"] is None
    assert parsed["nearest_distance"] is None


def test_intent_gap_record_dedup_skipped_case():
    """5min 內已 ack 過同類 gap → 仍寫 log（保留資料），但 acknowledged=False, ack_text=None。"""
    rec = _sample_record(ack_text=None, acknowledged=False)
    assert rec.acknowledged is False
    assert rec.ack_text is None
    # 但其他欄位完整保留，daily ritual 才看得到頻率
    assert rec.intent_type == "replay_user_history"
    assert rec.nearest_agent == "music_v2"

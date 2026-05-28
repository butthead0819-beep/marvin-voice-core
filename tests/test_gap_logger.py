"""GapLogger — agent_gaps.jsonl append writer + in-memory ack dedup。

設計：
- write() 永遠寫（包括 UNKNOWN / dedup-skipped 的 record，daily ritual 才看得到頻率）
- should_ack/mark_acked 操作 in-memory (speaker, intent_type) → last_ack_ts cache
- 5min 視窗短，process restart 重來無妨
- caller（voice_controller）自己決定 UNKNOWN 不 ack（GapLogger 不知道語意）
"""
import json
from pathlib import Path

from intent_gap import GapLogger, IntentGapRecord


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
        ack_text="這個我還沒會。",
        acknowledged=True,
    )
    base.update(overrides)
    return IntentGapRecord(**base)


# ── write() ──────────────────────────────────────────────────────────────────

def test_write_appends_record_as_jsonl_line(tmp_path: Path):
    path = tmp_path / "agent_gaps.jsonl"
    logger = GapLogger(path)

    logger.write(_sample_record())

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["speaker"] == "alice"
    assert parsed["intent_type"] == "replay_user_history"
    assert parsed["schema_version"] == 1


def test_write_appends_does_not_overwrite(tmp_path: Path):
    path = tmp_path / "agent_gaps.jsonl"
    logger = GapLogger(path)

    logger.write(_sample_record(utterance_id="u-1"))
    logger.write(_sample_record(utterance_id="u-2"))

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["utterance_id"] == "u-1"
    assert json.loads(lines[1])["utterance_id"] == "u-2"


def test_write_creates_parent_dir_if_missing(tmp_path: Path):
    """records/ 在 fresh checkout / fresh env 不一定存在。"""
    path = tmp_path / "nested" / "records" / "agent_gaps.jsonl"
    assert not path.parent.exists()

    GapLogger(path).write(_sample_record())

    assert path.exists()


def test_write_preserves_unicode_in_file(tmp_path: Path):
    path = tmp_path / "agent_gaps.jsonl"
    GapLogger(path).write(_sample_record())

    content = path.read_text(encoding="utf-8")
    assert "播我點過的歌" in content
    assert "\\u" not in content


# ── should_ack / mark_acked ──────────────────────────────────────────────────

def test_should_ack_returns_true_for_fresh_pair(tmp_path: Path):
    logger = GapLogger(tmp_path / "g.jsonl")
    assert logger.should_ack("alice", "replay_user_history", now=1000.0) is True


def test_should_ack_returns_false_within_dedup_window(tmp_path: Path):
    """拍板 #1：5 分鐘 dedup。"""
    logger = GapLogger(tmp_path / "g.jsonl", dedup_window_s=300.0)
    logger.mark_acked("alice", "replay_user_history", now=1000.0)

    # 4 分鐘後同 pair → 不該 ack
    assert logger.should_ack("alice", "replay_user_history", now=1240.0) is False


def test_should_ack_returns_true_after_dedup_window(tmp_path: Path):
    logger = GapLogger(tmp_path / "g.jsonl", dedup_window_s=300.0)
    logger.mark_acked("alice", "replay_user_history", now=1000.0)

    # 6 分鐘後 → 又可以 ack
    assert logger.should_ack("alice", "replay_user_history", now=1360.0) is True


def test_should_ack_is_per_speaker_intent_pair(tmp_path: Path):
    """不同 speaker 或不同 intent_type 不互相影響。"""
    logger = GapLogger(tmp_path / "g.jsonl", dedup_window_s=300.0)
    logger.mark_acked("alice", "replay_user_history", now=1000.0)

    # 同 speaker、不同 intent_type → 可 ack
    assert logger.should_ack("alice", "show_lyrics", now=1100.0) is True
    # 不同 speaker、同 intent_type → 可 ack
    assert logger.should_ack("bob", "replay_user_history", now=1100.0) is True


def test_mark_acked_cleans_stale_entries(tmp_path: Path):
    """ack cache 不該無限長大；超過 2x dedup_window 的 entry 順手清掉。"""
    logger = GapLogger(tmp_path / "g.jsonl", dedup_window_s=300.0)
    logger.mark_acked("ghost", "old_intent", now=0.0)
    assert ("ghost", "old_intent") in logger._last_ack

    # 1 小時後 mark 另一個 → 順手清 stale
    logger.mark_acked("alice", "new_intent", now=3600.0)
    assert ("ghost", "old_intent") not in logger._last_ack
    assert ("alice", "new_intent") in logger._last_ack

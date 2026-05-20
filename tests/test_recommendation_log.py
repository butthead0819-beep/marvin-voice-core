"""TDD: Recommendation dataclass + jsonl append logger.

5/21 slice 延伸：把主動推薦（music curation / topic suggestion / ...）
存成 append-only event log，供隔日離線 feedback batch 分析使用。

Zero prod wiring — 純資料 schema + IO helper。caller 之後再接。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from intent_agents.recommendation import (
    Recommendation,
    append_recommendation,
    read_recommendations,
)


def _sample(speaker: str = "大肚", agent: str = "music",
            ts: float | None = None) -> Recommendation:
    return Recommendation(
        ts=ts if ts is not None else time.time(),
        agent=agent,
        speaker=speaker,
        trigger="queue_empty",
        selected="周杰倫 夜曲",
        reason_internal="late_night+age_35+recent_excl_周杰倫",
        explanation_uttered="猜你想聽周杰倫，深夜配你的年代",
        feedback_window_s=300,
        channel_state={"members": ["大肚", "露"], "mood": "reflective"},
    )


# ── 1. Frozen / hashable / clean schema ────────────────────────────────────

def test_recommendation_is_frozen():
    """避免 caller 改寫 — 留檔資料必須 immutable。"""
    rec = _sample()
    with pytest.raises((AttributeError, TypeError, Exception)):
        rec.selected = "別的歌"  # type: ignore[misc]


def test_recommendation_to_jsonline_roundtrip():
    rec = _sample(ts=1716270000.0)
    line = rec.to_jsonline()
    parsed = json.loads(line)
    assert parsed["ts"] == 1716270000.0
    assert parsed["agent"] == "music"
    assert parsed["speaker"] == "大肚"
    assert parsed["selected"] == "周杰倫 夜曲"
    assert parsed["reason_internal"].startswith("late_night")
    assert parsed["channel_state"]["mood"] == "reflective"
    # jsonline 不該有換行（一行一筆是約定）
    assert "\n" not in line


# ── 2. Append-only writer ──────────────────────────────────────────────────

def test_append_creates_jsonl_with_one_line(tmp_path):
    log = tmp_path / "agent_recommendations.jsonl"
    rec = _sample(ts=1716270000.0)
    append_recommendation(rec, path=log)

    content = log.read_text(encoding="utf-8")
    assert content.endswith("\n"), "每筆 record 必須以換行收尾"
    assert content.count("\n") == 1
    parsed = json.loads(content.strip())
    assert parsed["selected"] == "周杰倫 夜曲"


def test_append_accumulates_multiple_records(tmp_path):
    log = tmp_path / "agent_recommendations.jsonl"
    for i in range(3):
        append_recommendation(_sample(ts=float(1716270000 + i)), path=log)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    assert [p["ts"] for p in parsed] == [1716270000.0, 1716270001.0, 1716270002.0]


def test_append_creates_parent_dir_if_missing(tmp_path):
    log = tmp_path / "deep" / "nested" / "agent_recommendations.jsonl"
    append_recommendation(_sample(ts=1716270000.0), path=log)
    assert log.exists()


def test_append_swallows_io_error_logs_warning(tmp_path, caplog):
    """寫入失敗不該炸到 prod — caller 在 wake path 上，不能因 log 失敗中斷推薦。"""
    import logging
    caplog.set_level(logging.WARNING)

    # 故意指一個不可寫的位置（已存在但是目錄）
    bad_path = tmp_path / "not_writable_dir"
    bad_path.mkdir()  # path 是目錄，open(... "a") 會失敗

    # 不該 raise
    append_recommendation(_sample(), path=bad_path)
    # 但要 log warning
    assert any("[Recommendation]" in r.message for r in caplog.records)


# ── 3. Reader (for offline batch) ──────────────────────────────────────────

def test_read_recommendations_returns_typed_records(tmp_path):
    log = tmp_path / "agent_recommendations.jsonl"
    append_recommendation(_sample(speaker="大肚", ts=100.0), path=log)
    append_recommendation(_sample(speaker="露", ts=200.0), path=log)

    out = list(read_recommendations(path=log))
    assert len(out) == 2
    assert all(isinstance(r, Recommendation) for r in out)
    assert out[0].speaker == "大肚"
    assert out[1].speaker == "露"
    assert out[1].ts == 200.0


def test_read_skips_corrupted_lines(tmp_path):
    """JSONL 內某一行壞掉不該整個讀檔失敗 — 跳過壞行即可。"""
    log = tmp_path / "agent_recommendations.jsonl"
    append_recommendation(_sample(ts=100.0), path=log)
    # 手動寫一行壞 JSON
    with log.open("a", encoding="utf-8") as f:
        f.write("{this is not json\n")
    append_recommendation(_sample(ts=200.0), path=log)

    out = list(read_recommendations(path=log))
    assert len(out) == 2  # 第二行被跳過，剩兩筆


def test_read_missing_file_returns_empty(tmp_path):
    """檔案不存在時不該炸 — 第一次跑離線 batch 時必須能正常 yield 空。"""
    out = list(read_recommendations(path=tmp_path / "no_such.jsonl"))
    assert out == []


# ── 4. Schema completeness ─────────────────────────────────────────────────

def test_recommendation_required_fields_present():
    """確保所有離線分析會用到的欄位都在 schema 內。"""
    rec = _sample()
    # 必填
    assert rec.agent
    assert rec.speaker
    assert rec.trigger
    assert rec.selected
    assert rec.reason_internal
    assert rec.feedback_window_s > 0
    # 選填但常見
    assert rec.explanation_uttered is not None
    assert isinstance(rec.channel_state, dict)


def test_explanation_uttered_can_be_empty_string():
    """有時候推薦沒講話（純後台動作）— explanation 允許空字串而非 None。"""
    rec = Recommendation(
        ts=1.0, agent="music", speaker="大肚", trigger="queue_empty",
        selected="x", reason_internal="y",
        explanation_uttered="",  # 允許
        feedback_window_s=300,
        channel_state={},
    )
    assert rec.explanation_uttered == ""

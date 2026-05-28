"""RescueOutcomeLogger — records/rescue_outcomes.jsonl append writer。

設計：
- 永遠寫（包括 shadow / unmatched record，daily ritual 才看得到頻率）
- 不 dedup（每筆都有分析價值，dedup 是 daily ritual 的工作）
- 缺資料夾自動建（jsonl 是 ML 流水線常見的 sink，路徑 forgiving）
- callable.write 直接給 IntentBus rescue_outcome_sink 用
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from intent_agents.rescue_outcome_logger import RescueOutcomeLogger


def _sample(**overrides):
    base = {
        "original_query": "希望下次可以找到好聽的歌",
        "rewritten_query": "下一首",
        "winner_agent": "skip",
        "winner_reason": "skip",
        "pragmatic_signal": "negative",
        "pragmatic_target": "current_song",
        "gap_class": "divergent",
        "speaker": "Alice",
        "ts": 12345.0,
    }
    base.update(overrides)
    return base


def test_write_appends_record_as_jsonl_line(tmp_path: Path):
    path = tmp_path / "rescue.jsonl"
    logger = RescueOutcomeLogger(path)
    logger.write(_sample())

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["gap_class"] == "divergent"
    assert parsed["original_query"] == "希望下次可以找到好聽的歌"


def test_write_appends_does_not_overwrite(tmp_path: Path):
    """連續兩筆都該保留——daily ritual 用次數做 cluster 門檻。"""
    path = tmp_path / "rescue.jsonl"
    logger = RescueOutcomeLogger(path)
    logger.write(_sample(gap_class="convergent"))
    logger.write(_sample(gap_class="unmatched"))

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["gap_class"] == "convergent"
    assert json.loads(lines[1])["gap_class"] == "unmatched"


def test_write_creates_parent_directory(tmp_path: Path):
    """records/ 第一次啟動可能不存在；不能因為缺資料夾就吞掉所有 rescue 訊號。"""
    path = tmp_path / "nested" / "records" / "rescue.jsonl"
    logger = RescueOutcomeLogger(path)
    logger.write(_sample())
    assert path.exists()


def test_write_uses_utf8_for_chinese(tmp_path: Path):
    """中文不能被 escape 成 \\uXXXX，否則 daily ritual diff 不可讀。"""
    path = tmp_path / "rescue.jsonl"
    logger = RescueOutcomeLogger(path)
    logger.write(_sample(original_query="這首歌真好聽（反話）"))
    line = path.read_text(encoding="utf-8").splitlines()[0]
    assert "這首歌真好聽" in line  # 不是 　...

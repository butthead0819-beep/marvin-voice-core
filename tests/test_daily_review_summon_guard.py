"""Summon 觸發 daily review 的 once-per-day guard——防同日多次 summon 重跑、重複付費。

設計（2026-07-08 使用者）：launchd 日排程脆弱(07-06 後靜默停 fire)→改當天第一次 summon
背景跑 review(不擋登場、成敗印 bot log)。guard＝完成標記 quality_metrics_<today>.md 存在
就跳過(launchd 或先前 summon 已跑)。付費記帳沿用 call_paid_review(cwd 對→寫同一帳本)。
"""
from cogs.voice_controller_connection import ConnectionMixin

_done = ConnectionMixin._daily_review_done_today


def test_done_when_marker_exists(tmp_path):
    (tmp_path / "quality_metrics_2026-07-08.md").write_text("x")
    assert _done("2026-07-08", str(tmp_path)) is True


def test_not_done_when_marker_absent(tmp_path):
    assert _done("2026-07-08", str(tmp_path)) is False


def test_not_done_when_only_other_date(tmp_path):
    (tmp_path / "quality_metrics_2026-07-07.md").write_text("x")
    assert _done("2026-07-08", str(tmp_path)) is False

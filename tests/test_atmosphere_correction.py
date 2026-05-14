"""
AtmosphereTracker.record_correction 的 TDD 測試。

校正資料持久化到 memory_manager.get_atmosphere_calibration() 同一條路徑，
不另發明檔案格式：memory_manager 需提供
  - get_atmosphere_calibration() → dict
  - record_atmosphere_correction(snapshot_ts, label, speaker) → None
（後者由 record_correction 透過 memory_manager 直接寫入）
"""

import time
import pytest

from marvin_voice_core.atmosphere_tracker import AtmosphereTracker


class _StubMemory:
    """測試替身：模擬 SukiMemory 的 atmosphere 校正介面。"""

    def __init__(self, calibration: dict | None = None):
        self.calibration = calibration or {}
        self.corrections: list[dict] = []

    def get_atmosphere_calibration(self) -> dict:
        return self.calibration

    def record_atmosphere_correction(self, snapshot_ts: float, label: str, speaker: str | None) -> None:
        self.corrections.append({
            "snapshot_ts": snapshot_ts,
            "label": label,
            "speaker": speaker,
        })


def test_record_correction_with_no_history_no_op(caplog):
    """從未有語料時呼叫 record_correction 不應該炸，只 log。"""
    tracker = AtmosphereTracker(memory_manager=None)
    # 不該拋例外
    tracker.record_correction(snapshot_ts=time.time(), label="too_loud")


def test_record_correction_persists_label():
    memory = _StubMemory()
    tracker = AtmosphereTracker(memory_manager=memory)
    tracker.add_utterance("alice", "今天工作好累")

    ts = time.time()
    tracker.record_correction(snapshot_ts=ts, label="too_loud", speaker="alice")

    assert len(memory.corrections) == 1
    rec = memory.corrections[0]
    assert rec["label"] == "too_loud"
    assert rec["speaker"] == "alice"
    assert rec["snapshot_ts"] == ts


def test_record_correction_handles_stale_ts(caplog):
    """超過 10 分鐘的 snapshot_ts 仍接受，但要 log warning。"""
    memory = _StubMemory()
    tracker = AtmosphereTracker(memory_manager=memory)
    tracker.add_utterance("alice", "嗨")

    stale_ts = time.time() - 11 * 60  # 11 分鐘前
    with caplog.at_level("WARNING"):
        tracker.record_correction(snapshot_ts=stale_ts, label="too_sharp")

    assert len(memory.corrections) == 1
    assert any("stale" in r.message.lower() or "過期" in r.message for r in caplog.records)


def test_record_correction_unknown_label_raises():
    memory = _StubMemory()
    tracker = AtmosphereTracker(memory_manager=memory)

    with pytest.raises(ValueError):
        tracker.record_correction(snapshot_ts=time.time(), label="too_weird")

    assert memory.corrections == []


def test_load_calibration_reads_corrections():
    """_load_calibration 應該能從 memory_manager 撈出校正關鍵字。"""
    memory = _StubMemory(calibration={"work": ["KPI", "OKR"]})
    tracker = AtmosphereTracker(memory_manager=memory)

    # 內建 work 關鍵字應已包含新增項
    assert "KPI" in tracker._topic_keywords["work"]
    assert "OKR" in tracker._topic_keywords["work"]

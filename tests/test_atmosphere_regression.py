"""
回歸測試：凍結 AtmosphereTracker.get_snapshot().to_prompt_str() 輸出。

目的：在加入 record_correction 之前先鎖住現有行為，
若日後加入校正流程不小心影響到「無校正情況」的輸出，這個測試會立刻紅。
"""

from marvin_voice_core.atmosphere_tracker import AtmosphereTracker


# 五句固定語料，跨三位說話者，混合 work / drinking / casual 話題與笑聲
_CANONICAL_UTTERANCES = [
    ("alice",   "今天工作好累，老闆又一直開會討論專案",       1_000_000.0),
    ("bob",     "對啊加班到爆 deadline 又快到了",              1_000_001.0),
    ("alice",   "晚上要不要去喝一杯啤酒放鬆",                    1_000_002.0),
    ("carol",   "好啊哈哈哈聽起來不錯 笑死",                      1_000_003.0),
    ("bob",     "走吧買酒去乾杯",                                  1_000_004.0),
]


def _build_tracker_with_fixed_clock(monkeypatch) -> AtmosphereTracker:
    """強制 time.time() 回固定值，避免 _prune 把語料清掉。"""
    fixed_now = 1_000_005.0
    monkeypatch.setattr(
        "marvin_voice_core.atmosphere_tracker.time.time",
        lambda: fixed_now,
    )
    return AtmosphereTracker(memory_manager=None)


def _feed_canonical(tracker: AtmosphereTracker) -> str:
    for speaker, text, ts in _CANONICAL_UTTERANCES:
        tracker.add_utterance(speaker, text, ts=ts)
    return tracker.get_snapshot().to_prompt_str()


# 凍結基線字串（依當前實作產生，未來變動需顯式更新）
_BASELINE = (
    "[當前氣氛] 當前話題：drinking（信心 40%） | 氣氛：飲酒作樂 "
    "| alice、bob 可能在喝酒 | carol 情緒高昂"
)


def test_snapshot_unchanged_with_no_corrections(monkeypatch):
    tracker = _build_tracker_with_fixed_clock(monkeypatch)
    output = _feed_canonical(tracker)
    assert output == _BASELINE


def test_snapshot_is_deterministic(monkeypatch):
    """同一序列重跑兩次必須得到相同字串。"""
    tracker_a = _build_tracker_with_fixed_clock(monkeypatch)
    tracker_b = _build_tracker_with_fixed_clock(monkeypatch)
    assert _feed_canonical(tracker_a) == _feed_canonical(tracker_b)

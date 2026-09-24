"""departure_predictor.py 單元測試——純邏輯，不碰真的 departure_cues.json。"""

import json

import pytest

from departure_predictor import (
    CUE_EPISODE_GAP_S,
    CUE_OUTCOME_WINDOW_S,
    DeparturePredictor,
    LAST_WORDS_MAX,
    MIN_SAMPLES,
    PREFAREWELL_COOLDOWN_S,
    DEPARTURE_CUE_RE,
    rejoin_action,
)


# ── 1. rejoin_action ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("gap, expected", [
    (None, "full"),
    (30, "silent"),
    (119, "silent"),
    (120, "welcome_back"),
    (3599, "welcome_back"),
    (3600, "full"),
])
def test_rejoin_action(gap, expected):
    assert rejoin_action(gap) == expected


# ── 2. regex 命中 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "我要先下線了",
    "晚安拜拜",
    "好了我要去當奶爸了",
    "我先關機了",
])
def test_cue_regex_hits(text):
    assert DEPARTURE_CUE_RE.search(text)


@pytest.mark.parametrize("text", [
    "今天天氣不錯",
    "下了一場雨",
])
def test_cue_regex_misses(text):
    assert not DEPARTURE_CUE_RE.search(text)


# ── 3. 命中率不足不觸發 ────────────────────────────────────────────────────

def _settle(pred, speaker, n_hit, n_miss, start_ts=1_000_000.0):
    """造 n_hit 中 + n_miss 不中的已結算 cue（各自間隔夠遠避免 episode 合併）。"""
    ts = start_ts
    for _ in range(n_hit):
        pred.observe(speaker, "我先下線了", ts)
        pred.on_leave(speaker, ts + 10, persist=False)  # 10s 後離開 → left=True
        ts += CUE_EPISODE_GAP_S + 10
    for _ in range(n_miss):
        pred.observe(speaker, "我先下線了", ts)
        pred._expire(speaker, ts + CUE_OUTCOME_WINDOW_S + 1)  # 逾時未離開 → left=False
        ts += CUE_EPISODE_GAP_S + 10
    return ts


def test_precision_threshold_triggers_at_50_percent(tmp_path):
    pred = DeparturePredictor(path=str(tmp_path / "c.json"))
    last_ts = _settle(pred, "A", n_hit=4, n_miss=4)
    n, p = pred.precision("A")
    assert n == 8
    assert p == pytest.approx(0.5)
    assert pred.should_prefarewell("A", last_ts) is True


def test_precision_threshold_blocks_below_50_percent(tmp_path):
    pred = DeparturePredictor(path=str(tmp_path / "c.json"))
    last_ts = _settle(pred, "B", n_hit=3, n_miss=5)
    n, p = pred.precision("B")
    assert n == 8
    assert p == pytest.approx(0.375)
    assert pred.should_prefarewell("B", last_ts) is False


def test_sample_count_below_min_samples_blocks_even_if_all_hit(tmp_path):
    pred = DeparturePredictor(path=str(tmp_path / "c.json"))
    last_ts = _settle(pred, "C", n_hit=7, n_miss=0)
    n, p = pred.precision("C")
    assert n == 7
    assert n < MIN_SAMPLES
    assert p == 1.0
    assert pred.should_prefarewell("C", last_ts) is False


# ── 4. 結算 ─────────────────────────────────────────────────────────

def test_on_leave_within_window_marks_hit(tmp_path):
    pred = DeparturePredictor(path=str(tmp_path / "c.json"))
    pred.observe("D", "晚安拜拜", 1000.0)
    pred.on_leave("D", 1000.0 + 200, persist=False)
    n, p = pred.precision("D")
    assert n == 1
    assert p == 1.0


def test_on_leave_after_window_marks_miss(tmp_path):
    pred = DeparturePredictor(path=str(tmp_path / "c.json"))
    pred.observe("E", "晚安拜拜", 1000.0)
    pred.on_leave("E", 1000.0 + 400, persist=False)
    n, p = pred.precision("E")
    assert n == 1
    assert p == 0.0


# ── 5. episode 合併 ─────────────────────────────────────────────────────────

def test_episode_merge_within_gap(tmp_path):
    pred = DeparturePredictor(path=str(tmp_path / "c.json"))
    pred.observe("F", "我先下線了", 1000.0)
    pred.observe("F", "晚安", 1000.0 + 60)  # 60s < CUE_EPISODE_GAP_S(120) → 併入同一段
    cues = pred._data["F"]["cues"]
    assert len(cues) == 1

    pred.observe("F", "拜拜", 1000.0 + 60 + 130)  # 130s 後 → 記第 2 筆
    cues = pred._data["F"]["cues"]
    assert len(cues) == 2


# ── 6. 冷卻 ─────────────────────────────────────────────────────────

def test_cooldown_blocks_within_window_then_recovers(tmp_path):
    pred = DeparturePredictor(path=str(tmp_path / "c.json"))
    last_ts = _settle(pred, "G", n_hit=4, n_miss=4)
    pred.mark_farewelled("G", last_ts)

    assert pred.should_prefarewell("G", last_ts + PREFAREWELL_COOLDOWN_S - 1) is False
    assert pred.should_prefarewell("G", last_ts + PREFAREWELL_COOLDOWN_S) is True


# ── 7. last_words ─────────────────────────────────────────────────────────

def test_last_words_records_recent_and_persists(tmp_path):
    path = str(tmp_path / "c.json")
    pred = DeparturePredictor(path=path)

    base = 1_000_000.0
    # 7 句話，全部落在 180s 窗內，但 LAST_WORDS_MAX=5 只留最後 5 句
    for i in range(7):
        pred.observe("H", f"話{i}", base + i * 20)
    leave_ts = base + 7 * 20

    pred.on_leave("H", leave_ts, persist=True)

    entry = pred._data["H"]["last_words"][-1]
    assert entry["texts"] == [f"話{i}" for i in range(2, 7)]
    assert len(entry["texts"]) == LAST_WORDS_MAX
    assert "hour" in entry and "weekday" in entry

    # 存檔後新 instance 讀得回來
    pred2 = DeparturePredictor(path=path)
    assert pred2._data["H"]["last_words"][-1]["texts"] == entry["texts"]


# ── 8. persist=False 不寫檔 ─────────────────────────────────────────────────

def test_persist_false_does_not_write_file(tmp_path):
    path = tmp_path / "c.json"
    pred = DeparturePredictor(path=str(path))
    pred.observe("I", "晚安拜拜", 1000.0)
    pred.on_leave("I", 1000.0 + 10, persist=False)
    assert not path.exists()

    pred.save()
    assert path.exists()

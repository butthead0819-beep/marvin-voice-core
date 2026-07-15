"""TDD: dj_tail_schedule.tail_dj_fire_delay 所有邊界案例（滑動窗語意）。

滑動窗：歌1 結束前 lead_s 秒點火，DJ 疊歌1尾段、溢進歌2開頭。
fire_at = duration - lead_s（與 DJ 長度無關）。
"""
from __future__ import annotations

from dj_tail_schedule import tail_dj_fire_delay


# ── 正常路徑 ──────────────────────────────────────────────────────────────────

def test_normal_returns_correct_delay():
    """duration=200, elapsed=0, lead=5 → fire_at=195 → delay=195.0"""
    result = tail_dj_fire_delay(200.0, 0.0, lead_s=5.0)
    assert result == 195.0


def test_normal_elapsed_nonzero():
    """duration=200, elapsed=100, lead=5 → fire_at=195 → delay=95.0"""
    result = tail_dj_fire_delay(200.0, 100.0, lead_s=5.0)
    assert result == 95.0


def test_default_lead_is_5s():
    """未給 lead_s → 預設 5s：duration=200, elapsed=0 → fire_at=195 → 195.0"""
    result = tail_dj_fire_delay(200.0, 0.0)
    assert result == 195.0


def test_return_type_is_float_for_valid():
    result = tail_dj_fire_delay(200.0, 0.0)
    assert isinstance(result, float)


# ── duration 無效 ─────────────────────────────────────────────────────────────

def test_duration_zero_returns_none():
    assert tail_dj_fire_delay(0, 0.0) is None


def test_duration_none_returns_none():
    assert tail_dj_fire_delay(None, 0.0) is None


# ── 歌太短 ────────────────────────────────────────────────────────────────────

def test_short_song_returns_none():
    """duration=25 < min_song_s=30 → None"""
    assert tail_dj_fire_delay(25.0, 0.0, min_song_s=30.0) is None


def test_exactly_min_song_passes():
    """duration==min_song_s 剛好等於門檻 → 不被過濾（只過濾 < min_song_s）"""
    # fire_at = 30 - 5 = 25; elapsed=0 → delay=25.0
    result = tail_dj_fire_delay(30.0, 0.0, lead_s=5.0, min_song_s=30.0)
    assert result == 25.0


# ── 已過窗 ────────────────────────────────────────────────────────────────────

def test_elapsed_past_fire_returns_none():
    """elapsed 超過 fire_at → None"""
    # fire_at = 100 - 5 = 95; elapsed=96 > 95 → None
    assert tail_dj_fire_delay(100.0, 96.0, lead_s=5.0) is None


def test_fire_at_equals_elapsed_returns_none():
    """fire_at 恰等於 elapsed（邊界）→ None"""
    # fire_at = 100 - 5 = 95; elapsed=95 → None
    assert tail_dj_fire_delay(100.0, 95.0, lead_s=5.0) is None


# ── 回傳 None 型別確認 ────────────────────────────────────────────────────────

def test_return_type_is_none_for_invalid():
    result = tail_dj_fire_delay(None, 0.0)
    assert result is None


def test_return_type_is_none_for_short():
    result = tail_dj_fire_delay(20.0, 0.0, min_song_s=30.0)
    assert result is None


# ── 邊緣值：delay 貼近 0 ──────────────────────────────────────────────────────

def test_delay_floors_to_zero():
    """fire_at > elapsed 但差值極小 → max(0, ...) 保證非負"""
    result = tail_dj_fire_delay(100.0, 94.9, lead_s=5.0)
    # fire_at=95, elapsed=94.9 → delay=0.1
    assert result is not None
    assert result >= 0.0
    assert abs(result - 0.1) < 1e-9


# ── 滑動窗特性：點火與 DJ 長度無關 ───────────────────────────────────────────

def test_fire_point_independent_of_dj_length():
    """滑動窗錨定歌尾 lead_s，不論 DJ 長短點火時刻相同（DJ 長度只決定溢進歌2多少）。"""
    # 無論 DJ 8s 或 20s，只要 lead 相同 → 同一 fire_at
    a = tail_dj_fire_delay(180.0, 0.0, lead_s=5.0)
    b = tail_dj_fire_delay(180.0, 0.0, lead_s=5.0)
    assert a == b == 175.0

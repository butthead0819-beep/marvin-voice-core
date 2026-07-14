"""
tests/test_car_open.py
TDD：車載開場時段解析（ESP32 puck 讀空氣開場的地基）。

5 個 bucket + 跨午夜（design doc / eng review）：
  morning     05–11
  noon        11–14
  afternoon   14–18
  evening     18–23
  late_night  23–05（跨午夜 wrap）
純函式、datetime 當參數傳（零 now() 依賴，可測）。
"""
import datetime as _dt
import pytest


def _at(hour):
    return _dt.datetime(2026, 7, 14, hour, 30, 0)


@pytest.mark.parametrize("hour,expected", [
    (5, "morning"), (7, "morning"), (10, "morning"),
    (11, "noon"), (12, "noon"), (13, "noon"),
    (14, "afternoon"), (16, "afternoon"), (17, "afternoon"),
    (18, "evening"), (20, "evening"), (22, "evening"),
    (23, "late_night"), (0, "late_night"), (3, "late_night"), (4, "late_night"),
])
def test_resolve_time_bucket_boundaries(hour, expected):
    from car_open import resolve_time_bucket
    assert resolve_time_bucket(_at(hour)) == expected


def test_resolve_time_bucket_returns_known_bucket_only():
    from car_open import resolve_time_bucket, TIME_BUCKETS
    for h in range(24):
        assert resolve_time_bucket(_at(h)) in TIME_BUCKETS


def test_midnight_wrap_late_night():
    """跨午夜：23:xx 與 00:xx–04:xx 同屬 late_night。"""
    from car_open import resolve_time_bucket
    assert resolve_time_bucket(_at(23)) == "late_night"
    assert resolve_time_bucket(_dt.datetime(2026, 7, 14, 0, 5)) == "late_night"
    assert resolve_time_bucket(_dt.datetime(2026, 7, 15, 4, 59)) == "late_night"
    # 05:00 整已跳出 late_night
    assert resolve_time_bucket(_dt.datetime(2026, 7, 15, 5, 0)) == "morning"

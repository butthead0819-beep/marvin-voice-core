"""Recommendation channel_state 豐富化 Phase 1：time_of_day_bucket 純函數.

對應 2026-05-28 推薦迭代改善 #1：豐富 channel_state 訊號 capture。
本檔 TDD time_of_day_bucket — 把 unix ts 轉成 morning / afternoon / evening / night
四 bucket，給離線 analyzer 分析「不同時段推薦的反應 pattern」。

時區：Asia/Taipei (UTC+8)，bot deploys 在台灣。
邊界（包左不包右）：
  05:00 ≤ morning   < 11:00
  11:00 ≤ afternoon < 17:00
  17:00 ≤ evening   < 22:00
  22:00 ≤ night     < 05:00 (跨日)
"""
from __future__ import annotations

import datetime

import pytest


_TPE = datetime.timezone(datetime.timedelta(hours=8))


def _ts_at(hour: int, minute: int = 0) -> float:
    """Unix ts for (hour:minute) UTC+8 on a fixed date (2026-05-28)."""
    return datetime.datetime(2026, 5, 28, hour, minute, tzinfo=_TPE).timestamp()


@pytest.mark.parametrize("hour,expected", [
    # morning 邊界
    (5, "morning"),
    (8, "morning"),
    (10, "morning"),
    # afternoon 邊界
    (11, "afternoon"),
    (13, "afternoon"),
    (16, "afternoon"),
    # evening 邊界
    (17, "evening"),
    (19, "evening"),
    (21, "evening"),
    # night 邊界（含跨日）
    (22, "night"),
    (23, "night"),
    (0, "night"),
    (3, "night"),
    (4, "night"),
])
def test_time_of_day_bucket_hour_buckets(hour, expected):
    from intent_agents.recommendation import time_of_day_bucket
    assert time_of_day_bucket(_ts_at(hour)) == expected, \
        f"hour={hour} 應該是 {expected}"


def test_time_of_day_bucket_exact_boundary_05_is_morning():
    """05:00:00 整 → morning（包左）。"""
    from intent_agents.recommendation import time_of_day_bucket
    assert time_of_day_bucket(_ts_at(5, 0)) == "morning"


def test_time_of_day_bucket_just_before_05_is_night():
    """04:59:59 → night（不包右側）。"""
    from intent_agents.recommendation import time_of_day_bucket
    ts = datetime.datetime(2026, 5, 28, 4, 59, 59, tzinfo=_TPE).timestamp()
    assert time_of_day_bucket(ts) == "night"


def test_time_of_day_bucket_exact_boundary_22_is_night():
    """22:00:00 整 → night（包左）。"""
    from intent_agents.recommendation import time_of_day_bucket
    assert time_of_day_bucket(_ts_at(22, 0)) == "night"


def test_time_of_day_bucket_just_before_22_is_evening():
    """21:59:59 → evening。"""
    from intent_agents.recommendation import time_of_day_bucket
    ts = datetime.datetime(2026, 5, 28, 21, 59, 59, tzinfo=_TPE).timestamp()
    assert time_of_day_bucket(ts) == "evening"


def test_time_of_day_bucket_uses_taipei_not_utc():
    """確認用 UTC+8 不是 UTC。UTC 2026-05-28 00:00 = UTC+8 08:00 → morning。
    若用 UTC 會算成 hour=0 → night，會錯。"""
    from intent_agents.recommendation import time_of_day_bucket
    utc_midnight = datetime.datetime(
        2026, 5, 28, 0, 0, tzinfo=datetime.timezone.utc,
    ).timestamp()
    assert time_of_day_bucket(utc_midnight) == "morning"

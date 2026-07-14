"""
tests/test_car_mode.py
TDD：車載模式 wiring（on_arrive 開場 / on_depart 停播 / TTL tick）。

car_mode.build_car_presence 把 CarPresence 的 on_arrive/on_depart 接到:
- on_arrive: resolve_time_bucket(now) → build_car_open → 呼叫注入的 play_open(car_open)
- on_depart: 呼叫注入的 stop_playback()
播放/停止是注入 callback（真實綁定在伺服器組裝處），故純邏輯可測、無 Discord/播放副作用。
"""
import datetime as _dt
import pytest
from unittest.mock import AsyncMock


def _clock():
    now = [0.0]
    return (lambda: now[0]), (lambda dt: now.__setitem__(0, now[0] + dt))


def _cand(title):
    from music_recommender import Candidate
    return Candidate(anchor_title=title, anchor_artist="x", lane="long_tail",
                     mode="direct", target_member=None, score=1.0)


@pytest.mark.asyncio
async def test_on_arrive_builds_open_and_calls_play():
    from car_mode import build_car_presence
    play_open, stop = AsyncMock(), AsyncMock()
    morning = _dt.datetime(2026, 7, 15, 8, 0)
    cp = build_car_presence(
        play_open=play_open, stop_playback=stop,
        pool_provider=lambda: [_cand("晴天")],
        now_fn=lambda: morning,
    )
    await cp.present()
    play_open.assert_awaited_once()
    co = play_open.call_args.args[0]
    assert co.song is not None and co.song.anchor_title == "晴天"
    assert isinstance(co.line, str) and co.line   # 有開場白（morning bucket）


@pytest.mark.asyncio
async def test_on_arrive_debounced_heartbeat_plays_once():
    from car_mode import build_car_presence
    play_open, stop = AsyncMock(), AsyncMock()
    cp = build_car_presence(
        play_open=play_open, stop_playback=stop,
        pool_provider=lambda: [_cand("稻香")],
        now_fn=lambda: _dt.datetime(2026, 7, 15, 8, 0),
    )
    await cp.present()      # 到達
    await cp.present()      # heartbeat
    play_open.assert_awaited_once()   # 開場只播一次


@pytest.mark.asyncio
async def test_on_depart_calls_stop():
    from car_mode import build_car_presence
    play_open, stop = AsyncMock(), AsyncMock()
    cp = build_car_presence(
        play_open=play_open, stop_playback=stop,
        pool_provider=lambda: [_cand("七里香")],
        now_fn=lambda: _dt.datetime(2026, 7, 15, 8, 0),
    )
    await cp.present()
    await cp.absent()
    stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_ttl_tick_stops_after_timeout():
    """熄火：heartbeat 停 → TTL 逾時 → tick 觸發 stop。"""
    from car_mode import build_car_presence, car_ttl_tick
    t, adv = _clock()
    play_open, stop = AsyncMock(), AsyncMock()
    cp = build_car_presence(
        play_open=play_open, stop_playback=stop,
        pool_provider=lambda: [_cand("晴天")],
        now_fn=lambda: _dt.datetime(2026, 7, 15, 8, 0),
        ttl_s=90.0, time_fn=t,
    )
    await cp.present()
    adv(91.0)
    fired = await car_ttl_tick(cp)
    assert fired is True
    stop.assert_awaited_once()


def test_default_open_lines_cover_all_buckets():
    from car_mode import DEFAULT_OPEN_LINES
    from car_open import TIME_BUCKETS
    for b in TIME_BUCKETS:
        assert DEFAULT_OPEN_LINES.get(b), f"bucket {b} 缺開場白"

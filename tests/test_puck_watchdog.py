"""
tests/test_puck_watchdog.py

TDD：car puck「完全沒聲音沒反應」偵測（見 puck_watchdog.py 開頭說明）。

check_puck_stall 純函式：兩種 stall 訊號（poll / deck）+ 車主不在車上就不判斷。
"""
from __future__ import annotations

import pytest

from puck_watchdog import check_puck_stall


def test_not_present_never_stalled_regardless_of_signals():
    """車主不在車上（熄火/沒配對）：puck 沒反應是正常的，不該警報。"""
    status = check_puck_stall(is_present=False, last_polled_ts=0.0, stall_seconds=999.0, now=1000.0)
    assert status.stalled is False
    assert status.reason is None


def test_present_but_never_polled_is_poll_stall():
    status = check_puck_stall(is_present=True, last_polled_ts=0.0, stall_seconds=None, now=1000.0)
    assert status.stalled is True
    assert status.reason == "poll"


def test_present_poll_within_threshold_not_stalled():
    status = check_puck_stall(
        is_present=True, last_polled_ts=990.0, stall_seconds=None, now=1000.0,
        poll_stall_threshold_s=20.0)
    assert status.stalled is False


def test_present_poll_beyond_threshold_is_poll_stall():
    status = check_puck_stall(
        is_present=True, last_polled_ts=970.0, stall_seconds=None, now=1000.0,
        poll_stall_threshold_s=20.0)
    assert status.stalled is True
    assert status.reason == "poll"


def test_present_polling_fine_but_deck_stalled():
    """輪詢正常（poll 訊號 ok），但下的播放指令太久沒被消費 → deck stall。"""
    status = check_puck_stall(
        is_present=True, last_polled_ts=999.0, stall_seconds=20.0, now=1000.0,
        poll_stall_threshold_s=20.0, deck_stall_threshold_s=15.0)
    assert status.stalled is True
    assert status.reason == "deck"


def test_present_polling_fine_and_deck_within_threshold_not_stalled():
    status = check_puck_stall(
        is_present=True, last_polled_ts=999.0, stall_seconds=5.0, now=1000.0,
        poll_stall_threshold_s=20.0, deck_stall_threshold_s=15.0)
    assert status.stalled is False
    assert status.reason is None


def test_present_deck_stall_none_means_no_pending_play_command():
    """stall_seconds=None（沒有待確認的播放指令，例如根本沒下過 play）→ 不算 deck stall。"""
    status = check_puck_stall(is_present=True, last_polled_ts=999.0, stall_seconds=None, now=1000.0)
    assert status.stalled is False


# ── PuckCommandQueue.stall_seconds() / mark_deck_hit()（活性訊號本身） ─────────

def test_stall_seconds_none_when_never_played():
    from marvin_voice_core.puck_command_queue import PuckCommandQueue

    q = PuckCommandQueue()
    assert q.stall_seconds() is None


def test_stall_seconds_counts_up_after_play_without_deck_hit():
    from marvin_voice_core.puck_command_queue import PuckCommandQueue

    q = PuckCommandQueue()
    q.play("https://youtu.be/a")
    s = q.stall_seconds(now=q._last_play_push_ts + 12.0)
    assert s == pytest.approx(12.0)


def test_stall_seconds_none_after_deck_hit_consumes_the_push():
    from marvin_voice_core.puck_command_queue import PuckCommandQueue

    q = PuckCommandQueue()
    q.play("https://youtu.be/a")
    q.mark_deck_hit()
    assert q.stall_seconds() is None


def test_stall_seconds_resets_on_next_play_after_deck_hit():
    """消費過一次後又下新指令：新指令沒被消費前，stall_seconds 要重新從新指令算起。

    q.play()/mark_deck_hit() 內部都用真實 time.time()，快速連續呼叫可能落在同一個
    時間戳（時鐘解析度撞在一起）——這裡直接操控內部欄位模擬「hit 發生在 push 之前」
    的正常情境，不依賴呼叫間隔真的能被時鐘量出差異。"""
    from marvin_voice_core.puck_command_queue import PuckCommandQueue

    q = PuckCommandQueue()
    q.play("https://youtu.be/a")
    q.last_deck_hit_ts = q._last_play_push_ts   # 模擬第一首已被消費
    q._last_play_push_ts = q.last_deck_hit_ts + 1.0   # 接著下第二首（queue_next）
    s = q.stall_seconds(now=q._last_play_push_ts + 3.0)
    assert s == pytest.approx(3.0)


def test_since_updates_last_polled_ts():
    from marvin_voice_core.puck_command_queue import PuckCommandQueue

    q = PuckCommandQueue()
    assert q.last_polled_ts == 0.0
    q.since(0)
    assert q.last_polled_ts > 0.0


# ── main_satellite._puck_watchdog_loop：串接迴圈本身（狀態轉換去重 + 注入測試點） ──

class _FakeCarPresence:
    def __init__(self, is_present=True):
        self.is_present = is_present


@pytest.mark.asyncio
async def test_watchdog_loop_dms_once_on_stall_and_once_on_recovery():
    from marvin_voice_core.puck_command_queue import PuckCommandQueue
    from main_satellite import _puck_watchdog_loop

    q = PuckCommandQueue()
    presence = _FakeCarPresence(is_present=True)
    dms = []
    clock = {"t": 1000.0}
    ticks = {"n": 0}

    async def fake_sleep(_):
        pass

    def fake_dm(text):
        dms.append(text)

    def fake_now():
        return clock["t"]

    def should_stop():
        ticks["n"] += 1
        # 第1拍：剛上車，grace period 起算不該 stall；第2拍：時間往前推超過
        # poll_stall_threshold 但 puck 仍沒輪詢過→poll stall；第3拍：模擬 puck 這時
        # 終於打了 /car_commands（直接設 last_polled_ts，q.since() 內部用真實
        # time.time() 沒法配合這裡注入的 fake clock）→ 應該恢復。
        if ticks["n"] == 2:
            clock["t"] += 30.0
        elif ticks["n"] == 3:
            q.last_polled_ts = clock["t"]
        return ticks["n"] > 4

    await _puck_watchdog_loop(
        presence, q, dm_fn=fake_dm, sleep_fn=fake_sleep, should_stop=should_stop, now_fn=fake_now)

    assert dms[0].startswith("🚨")
    assert any(d.startswith("✅") for d in dms)
    assert len(dms) == 2   # 只 alert 一次 + 只 recovered 一次，不重複洗版


@pytest.mark.asyncio
async def test_watchdog_loop_silent_when_never_stalled():
    from marvin_voice_core.puck_command_queue import PuckCommandQueue
    from main_satellite import _puck_watchdog_loop

    q = PuckCommandQueue()
    q.since(0)   # 一直有在正常輪詢
    presence = _FakeCarPresence(is_present=True)
    dms = []
    ticks = {"n": 0}

    async def fake_sleep(_):
        pass

    def should_stop():
        ticks["n"] += 1
        return ticks["n"] > 3

    await _puck_watchdog_loop(
        presence, q, dm_fn=lambda t: dms.append(t), sleep_fn=fake_sleep, should_stop=should_stop)

    assert dms == []


@pytest.mark.asyncio
async def test_watchdog_loop_not_present_stays_silent_even_if_stale():
    from marvin_voice_core.puck_command_queue import PuckCommandQueue
    from main_satellite import _puck_watchdog_loop

    q = PuckCommandQueue()   # 從沒輪詢過
    presence = _FakeCarPresence(is_present=False)   # 車主不在車上（熄火/沒配對）
    dms = []
    ticks = {"n": 0}

    async def fake_sleep(_):
        pass

    def should_stop():
        ticks["n"] += 1
        return ticks["n"] > 3

    await _puck_watchdog_loop(
        presence, q, dm_fn=lambda t: dms.append(t), sleep_fn=fake_sleep, should_stop=should_stop)

    assert dms == []


@pytest.mark.asyncio
async def test_watchdog_loop_dm_failure_does_not_crash_loop():
    """DM 送失敗（網路問題/token壞掉）不該讓整個 watchdog 迴圈掛掉。"""
    from marvin_voice_core.puck_command_queue import PuckCommandQueue
    from main_satellite import _puck_watchdog_loop

    q = PuckCommandQueue()   # 從沒輪詢過 → 一直是 poll stall
    presence = _FakeCarPresence(is_present=True)
    ticks = {"n": 0}

    async def fake_sleep(_):
        pass

    def boom(_text):
        raise RuntimeError("discord API 掛了")

    def should_stop():
        ticks["n"] += 1
        return ticks["n"] > 3

    await _puck_watchdog_loop(
        presence, q, dm_fn=boom, sleep_fn=fake_sleep, should_stop=should_stop)
    # 沒炸出去（await 沒拋例外）就算過

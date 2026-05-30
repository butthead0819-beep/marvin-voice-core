"""HotSwapCoordinator — 中途插 TTS 熱切換的狀態機。

最容易出 bug 的點：vc.stop() stream1 時 after_callback 會 fire。要能區分
「我主動切換造成的 stop」（不算歌結束，繼續等 stream2）vs「歌自然播完的 stop」
（真的結束）。判錯 → stream2 還沒播完就被當歌結束 → 音樂提早斷。

狀態流：
  idle → request(target) → (背景備 stream2) → set_stream2_ready(src)
       → ready_to_swap(pos>=target) → begin_swap() → swapping
       → finish_swap() → idle（stream2 變成新的當前播放）
"""
from __future__ import annotations

from hotswap_coordinator import HotSwapCoordinator


def test_idle_never_ready_to_swap():
    c = HotSwapCoordinator()
    assert c.ready_to_swap(current_position=999) is False
    assert c.is_swapping is False


def test_not_ready_until_stream2_set():
    """有 target 但 stream2 還沒備好 → 不能切（會切到空）。"""
    c = HotSwapCoordinator()
    c.request(target_seconds=95.0)
    assert c.ready_to_swap(current_position=96.0) is False  # 位置到了但 stream2 沒好


def test_not_ready_until_position_reached():
    """stream2 好了但還沒到切換點 → 不切。"""
    c = HotSwapCoordinator()
    c.request(target_seconds=95.0)
    c.set_stream2_ready("stream2_source")
    assert c.ready_to_swap(current_position=90.0) is False


def test_ready_when_position_reached_and_stream2_set():
    c = HotSwapCoordinator()
    c.request(target_seconds=95.0)
    c.set_stream2_ready("stream2_source")
    assert c.ready_to_swap(current_position=95.0) is True
    assert c.ready_to_swap(current_position=96.5) is True


def test_begin_swap_returns_stream2_and_sets_flag():
    c = HotSwapCoordinator()
    c.request(target_seconds=95.0)
    c.set_stream2_ready("stream2_source")
    src = c.begin_swap()
    assert src == "stream2_source"
    assert c.is_swapping is True


def test_not_ready_again_while_swapping():
    """切換進行中不該再次觸發（避免重複 stop/play）。"""
    c = HotSwapCoordinator()
    c.request(target_seconds=95.0)
    c.set_stream2_ready("s2")
    c.begin_swap()
    assert c.ready_to_swap(current_position=100.0) is False


def test_intentional_stop_detected_during_swap():
    """關鍵：swapping 中 after_callback 看 is_swapping=True → 知道這 stop 是我做的，
    不該當歌結束。"""
    c = HotSwapCoordinator()
    c.request(target_seconds=95.0)
    c.set_stream2_ready("s2")
    c.begin_swap()
    # after_callback 的判斷依據
    assert c.is_swapping is True


def test_finish_swap_returns_to_idle():
    """切換完成 → 回 idle，stream2 成為新當前播放，可接受下一次 request。"""
    c = HotSwapCoordinator()
    c.request(target_seconds=95.0)
    c.set_stream2_ready("s2")
    c.begin_swap()
    c.finish_swap()
    assert c.is_swapping is False
    assert c.ready_to_swap(current_position=999) is False  # 回 idle，無 pending target


def test_natural_song_end_not_swapping():
    """沒 request 過 → is_swapping=False → after_callback 視為歌自然結束（正常）。"""
    c = HotSwapCoordinator()
    assert c.is_swapping is False


def test_abort_clears_pending_request():
    """stream2 備料趕不上 / 取消 → abort 清掉 pending，不影響當前播放。"""
    c = HotSwapCoordinator()
    c.request(target_seconds=95.0)
    c.set_stream2_ready("s2")
    c.abort()
    assert c.ready_to_swap(current_position=100.0) is False
    assert c.is_swapping is False


def test_second_request_overrides_first():
    """同一首歌內第二次插話 request → 覆蓋前一個未觸發的（取最新）。"""
    c = HotSwapCoordinator()
    c.request(target_seconds=95.0)
    c.request(target_seconds=120.0)
    c.set_stream2_ready("s2")
    assert c.ready_to_swap(current_position=96.0) is False   # 舊 target 95 不算
    assert c.ready_to_swap(current_position=120.0) is True    # 新 target 120

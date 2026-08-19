"""TDD：cogs/music_cog.py::_safe_pi_bt_seek()

背景：youtube_heatmap.py::pick_highlight_start() 已經有「起點離結尾剩不到
min_remaining 秒就放棄」的安全檢查，但實機仍量到 seek=255.96s 這種幾乎跳到
歌曲尾端、只剩幾秒內容的異常值（duration ~260s）——上游那個檢查用的 duration
可能跟後續排程/播放實際用的 duration 不是同一份，導致原本該擋下的極端值漏過
去，pi_bt 端一 seek 過去幾乎立刻撞真 EOF，聽感是「必定提早結束」。在真正套用
seek 的這一刻重新驗證一次，不夠就當作沒有這個 highlight。
"""
from cogs.music_cog import _safe_pi_bt_seek


def test_returns_none_when_no_highlight():
    assert _safe_pi_bt_seek(200.0, None) is None
    assert _safe_pi_bt_seek(200.0, 0) is None


def test_returns_none_when_no_duration():
    assert _safe_pi_bt_seek(None, 20.0) is None
    assert _safe_pi_bt_seek(0, 20.0) is None


def test_returns_highlight_when_remaining_is_safe():
    assert _safe_pi_bt_seek(200.0, 20.0) == 20.0


def test_returns_none_when_remaining_too_short():
    """實機踩到的情境：duration=260, highlight=255.96 → 只剩 4.04s，遠低於
    45s 安全邊界，該當作沒有這個 highlight。"""
    assert _safe_pi_bt_seek(260.0, 255.96) is None


def test_boundary_exactly_at_min_remaining_passes():
    """duration - highlight == min_remaining（預設 45.0）→ 剛好打平不算「低於」，
    放行——跟 youtube_heatmap.py::pick_highlight_start() 用同一個比較方向
    （`<` 而非 `<=`），兩處邊界行為要一致。"""
    assert _safe_pi_bt_seek(245.0, 200.0, min_remaining=45.0) == 200.0


def test_custom_min_remaining():
    assert _safe_pi_bt_seek(100.0, 60.0, min_remaining=30.0) == 60.0
    assert _safe_pi_bt_seek(100.0, 80.0, min_remaining=30.0) is None

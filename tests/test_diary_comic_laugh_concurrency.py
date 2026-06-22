"""多人笑相對在場人數的精華閘：concurrent vocalizer 快照 → find_highlights gating。"""
from diary_comic.highlight import (
    enough_laughter, count_concurrent_voices, find_highlights)


# ---- enough_laughter：發聲人數 / 在場人數 比例閘 ----

def test_enough_laughter_intimate_room_one_voice_ok():
    # 在場 2 人 → 需要 1（親密對話，1 人笑就算）
    assert enough_laughter(vocalizers=1, present=2) is True


def test_enough_laughter_big_room_solo_chuckle_rejected():
    # 在場 5 人只有 1 人笑 → 陪笑，擋掉
    assert enough_laughter(vocalizers=1, present=5) is False


def test_enough_laughter_big_room_majority_ok():
    # 在場 5 人有 3 人笑 → 哄堂，過
    assert enough_laughter(vocalizers=3, present=5) is True


def test_enough_laughter_unknown_present_does_not_block():
    # 不知道在場（0）→ 不擋（資料缺失不懲罰）
    assert enough_laughter(vocalizers=1, present=0) is True


# ---- count_concurrent_voices：時間窗內有幾個不同 user 發聲 ----

def test_count_concurrent_voices_within_window():
    now = 1000.0
    last_spoken = {1: 999.0, 2: 998.5, 3: 990.0, 4: 0.0}  # 3 太舊、4 沒講過
    assert count_concurrent_voices(last_spoken, now=now, window=3.0) == 2


def test_count_concurrent_voices_empty():
    assert count_concurrent_voices({}, now=1000.0, window=3.0) == 0


# ---- find_highlights：有 laugh_events 時套比例閘 ----

def _rows():
    # 兩個笑點：t=100（showay 笑）、t=500（狗與露 笑）
    return [
        ("weakgogo", "我國中被打手掌", 98.0),
        ("showay", "哈哈哈哈哈哈哈", 100.0),
        ("showay", "講個成本的事", 498.0),
        ("狗與露", "哈哈哈哈哈", 500.0),
    ]


def test_find_highlights_no_events_keeps_all_backward_compat():
    hs = find_highlights(_rows())
    assert len(hs) == 2  # 沒 laugh_events → 行為不變


def test_find_highlights_events_drop_solo_chuckle():
    # t=500 那筆：在場 5 人只有 1 人發聲 → 擋；t=100：在場 2 人 1 發聲 → 留
    events = [
        {"speaker": "showay", "timestamp": 100.0, "vocalizers": 1, "present": 2},
        {"speaker": "狗與露", "timestamp": 500.0, "vocalizers": 1, "present": 5},
    ]
    hs = find_highlights(_rows(), laugh_events=events)
    assert [h.laugher for h in hs] == ["showay"]  # 只留下哄堂的


def test_find_highlights_event_missing_keeps_moment():
    # 只有 t=100 有 event 且擋不掉；t=500 無 event → 留（資料缺失不懲罰）
    events = [{"speaker": "showay", "timestamp": 100.0, "vocalizers": 2, "present": 2}]
    hs = find_highlights(_rows(), laugh_events=events)
    assert len(hs) == 2

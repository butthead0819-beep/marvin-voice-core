"""dj_mood_hint 純函式：日記氣氛行 → DJ 口白內心提示 + 防露餡檢查。

見 dj_mood_hint.py docstring：讀得到氣氛但不准講出來。
"""
from __future__ import annotations

import datetime as _dt
from unittest.mock import MagicMock

from dj_mood_hint import (
    MOOD_HINT_HEADER,
    build_mood_hint,
    leaks_mood,
    mood_hint_ctx_lines,
)
from dj_mood_hint import latest_mood as _latest_mood


def _ts(minutes_ago: float = 0):
    return (_dt.datetime.now() - _dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _entry(mood="", speakers=None, ts_str=None):
    e = MagicMock()
    e.mood = mood
    e.speakers = speakers if speakers is not None else []
    e.ts_str = ts_str if ts_str is not None else _ts()
    return e


# ── latest_mood ──────────────────────────────────────────────────────────────

def test_latest_mood_fresh_within_window():
    entries = [_entry(mood="熱絡", ts_str=_ts(5))]
    result = _latest_mood(entries, now=__import__("time").mktime(_dt.datetime.now().timetuple()))
    assert result == ("熱絡", [])


def test_latest_mood_too_old_returns_none_even_if_older_has_mood():
    import time
    older = _entry(mood="放空", ts_str=_ts(40))  # 更舊但有 mood
    newest = _entry(mood="熱絡", ts_str=_ts(30))  # 最新有 mood 的那筆已過期
    entries = [older, newest]
    now = time.mktime(_dt.datetime.now().timetuple())
    assert _latest_mood(entries, now=now) is None


def test_latest_mood_skips_empty_mood_falls_back_to_earlier_fresh_entry():
    import time
    earlier_with_mood = _entry(mood="放空", ts_str=_ts(5))
    latest_no_mood = _entry(mood="", ts_str=_ts(1))
    entries = [earlier_with_mood, latest_no_mood]
    now = time.mktime(_dt.datetime.now().timetuple())
    assert _latest_mood(entries, now=now) == ("放空", [])


def test_latest_mood_non_str_mood_skipped():
    import time
    e = _entry(ts_str=_ts(1))
    e.mood = MagicMock()  # 不是 str
    now = time.mktime(_dt.datetime.now().timetuple())
    assert _latest_mood([e], now=now) is None


def test_latest_mood_bad_ts_str_skipped():
    import time
    e = _entry(mood="熱絡", ts_str="not-a-timestamp")
    now = time.mktime(_dt.datetime.now().timetuple())
    assert _latest_mood([e], now=now) is None


def test_latest_mood_empty_list():
    import time
    assert _latest_mood([], now=time.time()) is None


# ── build_mood_hint ──────────────────────────────────────────────────────────

def test_build_mood_hint_never_includes_event_even_if_subject_present():
    """10/19：live 實測事件會被 LLM 換句話講出來（腰痛/找不到路），事件一律不給 LLM。"""
    hint = build_mood_hint("熱絡｜showay 不爽隊友講不聽", ["showay", "狗與露"], {"showay"})
    assert hint == "熱絡"


def test_build_mood_hint_drops_event_when_subject_absent():
    hint = build_mood_hint("熱絡｜showay 不爽隊友講不聽", ["showay", "狗與露"], {"狗與露"})
    assert hint == "熱絡"


def test_build_mood_hint_drops_tone_containing_present_name():
    assert build_mood_hint("showay主導", ["showay"], {"showay"}) == ""


def test_build_mood_hint_drops_tone_containing_diary_speaker_name():
    assert build_mood_hint("大肚碎念", ["大肚", "showay"], {"showay"}) == ""


def test_build_mood_hint_drops_tone_containing_non_roster_present_name():
    # 名冊外的新人：靠在場名單擋
    assert build_mood_hint("小明主導", [], {"小明"}) == ""


def test_build_mood_hint_drops_tone_containing_non_roster_speaker_name():
    # 名冊外的新人：在日記 speakers 裡但已離場，也不准出現
    assert build_mood_hint("阿凱碎念", ["阿凱", "showay"], {"showay"}) == ""


def test_build_mood_hint_drops_tone_containing_roster_name():
    # 名冊內的人就算不在在場名單、也不在日記 speakers 裡，也不准出現在提示裡
    assert build_mood_hint("weakgogo狂衝", [], {"showay"}) == ""


def test_build_mood_hint_event_only_gives_nothing():
    assert build_mood_hint("-｜showay 不爽", ["showay"], {"showay"}) == ""


def test_build_mood_hint_empty_when_nobody_from_diary_present():
    hint = build_mood_hint("熱絡｜showay 不爽隊友講不聽", ["showay", "狗與露"], {"大肚"})
    assert hint == ""


def test_build_mood_hint_empty_speakers_does_not_block():
    hint = build_mood_hint("放空", [], {"大肚"})
    assert hint == "放空"


def test_build_mood_hint_tone_only():
    hint = build_mood_hint("放空", [], None)
    assert hint == "放空"


def test_build_mood_hint_dash_tone_is_empty():
    hint = build_mood_hint("-", [], None)
    assert hint == ""


def test_build_mood_hint_halfwidth_separator():
    hint = build_mood_hint("熱絡|showay 不爽隊友講不聽", ["showay"], {"showay"})
    assert hint == "熱絡"


def test_build_mood_hint_present_none_does_not_crash():
    hint = build_mood_hint("放空", [], None)
    assert hint == "放空"


# ── mood_hint_ctx_lines ──────────────────────────────────────────────────────

def test_mood_hint_ctx_lines_empty():
    assert mood_hint_ctx_lines("") == []


def test_mood_hint_ctx_lines_non_empty():
    lines = mood_hint_ctx_lines("熱絡")
    assert len(lines) == 2
    assert lines[0].startswith(MOOD_HINT_HEADER)


# ── leaks_mood ───────────────────────────────────────────────────────────────

def test_leaks_mood_ngram_overlap():
    hint = "熱絡｜showay 不爽隊友講不聽"
    text = "有人遇到隊友講不聽，來首歌"
    assert leaks_mood(text, hint, set())


def test_leaks_mood_name_only_not_a_leak():
    hint = "熱絡｜showay 不爽隊友講不聽"
    text = "showay 點的歌來了"
    assert not leaks_mood(text, hint, {"showay"})


def test_leaks_mood_emotion_word_both_sides():
    hint = "大家都不爽"
    text = "氣氛有點不爽，來點輕快的"
    assert leaks_mood(text, hint, set())


def test_leaks_mood_emotion_word_only_one_side():
    hint = "大家都不爽"
    text = "有點難過，來點輕快的"
    assert not leaks_mood(text, hint, set())


def test_leaks_mood_ngram_ignores_punctuation():
    hint = "互相吐槽"
    text = "大家互相、吐槽得很開心"
    assert leaks_mood(text, hint, set())


def test_leaks_mood_empty_strings():
    assert not leaks_mood("", "熱絡", set())
    assert not leaks_mood("某句話", "", set())

"""換歌空檔記憶確認（memory_check.py）純函式測試。期望值一律寫死。"""
from __future__ import annotations

import memory_check as mc


# ── artist_names ─────────────────────────────────────────────────────────────

def test_artist_names_merges_core_and_per_user():
    fp = {
        "core_artists": [["周杰倫", 10], ["陶喆", 3]],
        "per_user": {
            "狗與露": {"core_artists": [["張雨生", 5], ["周杰倫", 2]]},
            "大肚": {"core_artists": [["茄子蛋", 1]]},
        },
    }
    assert mc.artist_names(fp) == {"周杰倫", "陶喆", "張雨生", "茄子蛋"}


def test_artist_names_broken_structure_returns_empty():
    assert mc.artist_names({}) == set()
    assert mc.artist_names(None) == set()
    assert mc.artist_names({"core_artists": "壞掉", "per_user": "壞掉"}) == set()
    assert mc.artist_names({"core_artists": [["周杰倫"]], "per_user": {"x": {"core_artists": [[]]}}}) == {"周杰倫"}


# ── matches_artist ───────────────────────────────────────────────────────────

def test_matches_artist_substring_both_ways():
    assert mc.matches_artist("張雨生音樂", {"張雨生"}) is True
    assert mc.matches_artist("茄子蛋", {"茄子蛋EggPlantEgg"}) is True
    assert mc.matches_artist("雞排", {"張雨生"}) is False
    assert mc.matches_artist("周", {"周杰倫"}) is False


# ── pick_question ─────────────────────────────────────────────────────────────

NOW = 1_000_000.0
ARTISTS = {"張雨生", "周杰倫"}


def _taste(score=5.0, last_update=NOW - 100):
    return {"score": score, "last_update": last_update}


def test_pick_question_silence_threshold():
    tastes = {"狗與露": {"張雨生": _taste()}}
    assert mc.pick_question(present=["狗與露"], tastes=tastes, taboos={}, artists=ARTISTS,
                             log_rows=[], now=NOW, last_speech_ts=NOW - 239) is None
    assert mc.pick_question(present=["狗與露"], tastes=tastes, taboos={}, artists=ARTISTS,
                             log_rows=[], now=NOW, last_speech_ts=NOW - 240) == ("狗與露", "張雨生")


def test_pick_question_global_gap_blocks():
    tastes = {"狗與露": {"張雨生": _taste()}}
    log_rows = [{"event": "asked", "ts": NOW - 29 * 60, "speaker": "大肚", "item": "周杰倫"}]
    assert mc.pick_question(present=["狗與露"], tastes=tastes, taboos={}, artists=ARTISTS,
                             log_rows=log_rows, now=NOW, last_speech_ts=NOW - 300) is None


def test_pick_question_per_speaker_gap_skips_to_other_present_person():
    tastes = {
        "狗與露": {"張雨生": _taste()},
        "大肚": {"周杰倫": _taste()},
    }
    log_rows = [{"event": "asked", "ts": NOW - 23 * 3600, "speaker": "狗與露", "item": "張雨生"}]
    result = mc.pick_question(present=["狗與露", "大肚"], tastes=tastes, taboos={}, artists=ARTISTS,
                               log_rows=log_rows, now=NOW, last_speech_ts=NOW - 300)
    assert result == ("大肚", "周杰倫")


def test_pick_question_item_reask_gate():
    tastes = {"狗與露": {"張雨生": _taste()}}
    log_29d = [{"event": "asked", "ts": NOW - 29 * 86400, "speaker": "狗與露", "item": "張雨生"}]
    assert mc.pick_question(present=["狗與露"], tastes=tastes, taboos={}, artists=ARTISTS,
                             log_rows=log_29d, now=NOW, last_speech_ts=NOW - 300) is None

    log_31d = [{"event": "asked", "ts": NOW - 31 * 86400, "speaker": "狗與露", "item": "張雨生"}]
    assert mc.pick_question(present=["狗與露"], tastes=tastes, taboos={}, artists=ARTISTS,
                             log_rows=log_31d, now=NOW, last_speech_ts=NOW - 300) == ("狗與露", "張雨生")


def test_pick_question_score_threshold():
    tastes_below = {"狗與露": {"張雨生": _taste(score=2.9)}}
    assert mc.pick_question(present=["狗與露"], tastes=tastes_below, taboos={}, artists=ARTISTS,
                             log_rows=[], now=NOW, last_speech_ts=NOW - 300) is None

    tastes_at = {"狗與露": {"張雨生": _taste(score=3.0)}}
    assert mc.pick_question(present=["狗與露"], tastes=tastes_at, taboos={}, artists=ARTISTS,
                             log_rows=[], now=NOW, last_speech_ts=NOW - 300) == ("狗與露", "張雨生")


def test_pick_question_taboo_excluded():
    tastes = {"狗與露": {"張雨生": _taste()}}
    taboos = {"狗與露": {"張雨生"}}
    assert mc.pick_question(present=["狗與露"], tastes=tastes, taboos=taboos, artists=ARTISTS,
                             log_rows=[], now=NOW, last_speech_ts=NOW - 300) is None


def test_pick_question_non_artist_item_excluded():
    tastes = {"狗與露": {"欣賞異性": _taste()}}
    assert mc.pick_question(present=["狗與露"], tastes=tastes, taboos={}, artists=ARTISTS,
                             log_rows=[], now=NOW, last_speech_ts=NOW - 300) is None


def test_pick_question_picks_least_recently_updated():
    tastes = {
        "狗與露": {"張雨生": _taste(last_update=NOW - 1000)},
        "大肚": {"周杰倫": _taste(last_update=NOW - 5000)},
    }
    result = mc.pick_question(present=["狗與露", "大肚"], tastes=tastes, taboos={}, artists=ARTISTS,
                               log_rows=[], now=NOW, last_speech_ts=NOW - 300)
    assert result == ("大肚", "周杰倫")


def test_pick_question_absent_person_not_considered():
    tastes = {
        "狗與露": {"張雨生": _taste()},
        "不在場的人": {"周杰倫": _taste(last_update=NOW - 999999)},
    }
    result = mc.pick_question(present=["狗與露"], tastes=tastes, taboos={}, artists=ARTISTS,
                               log_rows=[], now=NOW, last_speech_ts=NOW - 300)
    assert result == ("狗與露", "張雨生")


# ── question_text ─────────────────────────────────────────────────────────────

def test_question_text():
    assert mc.question_text("狗與露", "張雨生") == "狗與露，你還喜歡張雨生嗎？"


# ── classify_answer ───────────────────────────────────────────────────────────

def test_classify_answer():
    assert mc.classify_answer("對啊") == "yes"
    assert mc.classify_answer("還是喜歡") == "yes"
    assert mc.classify_answer("不喜歡了") == "no"
    assert mc.classify_answer("沒有啦") == "no"
    assert mc.classify_answer("還不錯") == "yes"
    assert mc.classify_answer("不是") == "no"
    assert mc.classify_answer("嗯…") == "unclear"
    assert mc.classify_answer("你說什麼") == "unclear"
    assert mc.classify_answer("早就不聽了") == "no"


# ── answer_window_open ─────────────────────────────────────────────────────────

def test_answer_window_open():
    window = {"speaker": "狗與露", "item": "張雨生", "deadline": NOW + 10}
    assert mc.answer_window_open(window, "大肚", NOW) is False
    assert mc.answer_window_open(window, "狗與露", NOW + 20) is False
    assert mc.answer_window_open(window, "狗與露", NOW) is True
    assert mc.answer_window_open(None, "狗與露", NOW) is False

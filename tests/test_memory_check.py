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
    assert mc.pick_question(present=["狗與露"], tastes=tastes, taboos={}, artists=ARTISTS, categories={},
                             log_rows=[], now=NOW, last_speech_ts=NOW - 239) is None
    assert mc.pick_question(present=["狗與露"], tastes=tastes, taboos={}, artists=ARTISTS, categories={},
                             log_rows=[], now=NOW, last_speech_ts=NOW - 240) == ("狗與露", "張雨生")


def test_pick_question_global_gap_blocks():
    tastes = {"狗與露": {"張雨生": _taste()}}
    log_rows = [{"event": "asked", "ts": NOW - 29 * 60, "speaker": "大肚", "item": "周杰倫"}]
    assert mc.pick_question(present=["狗與露"], tastes=tastes, taboos={}, artists=ARTISTS, categories={},
                             log_rows=log_rows, now=NOW, last_speech_ts=NOW - 300) is None


def test_pick_question_per_speaker_gap_skips_to_other_present_person():
    tastes = {
        "狗與露": {"張雨生": _taste()},
        "大肚": {"周杰倫": _taste()},
    }
    log_rows = [{"event": "asked", "ts": NOW - 23 * 3600, "speaker": "狗與露", "item": "張雨生"}]
    result = mc.pick_question(present=["狗與露", "大肚"], tastes=tastes, taboos={}, artists=ARTISTS, categories={},
                               log_rows=log_rows, now=NOW, last_speech_ts=NOW - 300)
    assert result == ("大肚", "周杰倫")


def test_pick_question_item_reask_gate():
    tastes = {"狗與露": {"張雨生": _taste()}}
    log_29d = [{"event": "asked", "ts": NOW - 29 * 86400, "speaker": "狗與露", "item": "張雨生"}]
    assert mc.pick_question(present=["狗與露"], tastes=tastes, taboos={}, artists=ARTISTS, categories={},
                             log_rows=log_29d, now=NOW, last_speech_ts=NOW - 300) is None

    log_31d = [{"event": "asked", "ts": NOW - 31 * 86400, "speaker": "狗與露", "item": "張雨生"}]
    assert mc.pick_question(present=["狗與露"], tastes=tastes, taboos={}, artists=ARTISTS, categories={},
                             log_rows=log_31d, now=NOW, last_speech_ts=NOW - 300) == ("狗與露", "張雨生")


def test_pick_question_score_threshold():
    tastes_below = {"狗與露": {"張雨生": _taste(score=2.9)}}
    assert mc.pick_question(present=["狗與露"], tastes=tastes_below, taboos={}, artists=ARTISTS, categories={},
                             log_rows=[], now=NOW, last_speech_ts=NOW - 300) is None

    tastes_at = {"狗與露": {"張雨生": _taste(score=3.0)}}
    assert mc.pick_question(present=["狗與露"], tastes=tastes_at, taboos={}, artists=ARTISTS, categories={},
                             log_rows=[], now=NOW, last_speech_ts=NOW - 300) == ("狗與露", "張雨生")


def test_pick_question_taboo_excluded():
    tastes = {"狗與露": {"張雨生": _taste()}}
    taboos = {"狗與露": {"張雨生"}}
    assert mc.pick_question(present=["狗與露"], tastes=tastes, taboos=taboos, artists=ARTISTS, categories={},
                             log_rows=[], now=NOW, last_speech_ts=NOW - 300) is None


def test_pick_question_non_artist_item_excluded():
    tastes = {"狗與露": {"欣賞異性": _taste()}}
    assert mc.pick_question(present=["狗與露"], tastes=tastes, taboos={}, artists=ARTISTS, categories={},
                             log_rows=[], now=NOW, last_speech_ts=NOW - 300) is None


def test_pick_question_picks_least_recently_updated():
    tastes = {
        "狗與露": {"張雨生": _taste(last_update=NOW - 1000)},
        "大肚": {"周杰倫": _taste(last_update=NOW - 5000)},
    }
    result = mc.pick_question(present=["狗與露", "大肚"], tastes=tastes, taboos={}, artists=ARTISTS, categories={},
                               log_rows=[], now=NOW, last_speech_ts=NOW - 300)
    assert result == ("大肚", "周杰倫")


def test_pick_question_absent_person_not_considered():
    tastes = {
        "狗與露": {"張雨生": _taste()},
        "不在場的人": {"周杰倫": _taste(last_update=NOW - 999999)},
    }
    result = mc.pick_question(present=["狗與露"], tastes=tastes, taboos={}, artists=ARTISTS, categories={},
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


def test_classify_answer_mixed_yes_no_is_unclear():
    """正反都有 → 聽不懂，不准刪（原則 5）。「不喜歡」裡的「喜歡」不算正面。"""
    assert mc.classify_answer("不是啦我還是喜歡") == "unclear"
    assert mc.classify_answer("對啊沒在聽了") == "unclear"
    assert mc.classify_answer("不喜歡了") == "no"


def test_classify_answer_command_or_long_text_is_unclear():
    """窗內講指令（含喚醒詞）或長句不是在回答 → 放行給正常流程。"""
    assert mc.classify_answer("對了馬文幫我放歌") == "unclear"
    assert mc.classify_answer("對啊我昨天在公司聽了一整天真的很喜歡") == "unclear"


# ── 10/26 第二版：輪流問在場者 + 可問遊戲/食物 ─────────────────────────────

def test_room_ready():
    assert mc.room_ready(log_rows=[], now=NOW, last_speech_ts=NOW - 239) is False
    assert mc.room_ready(log_rows=[], now=NOW, last_speech_ts=NOW - 240) is True
    asked = [{"ts": NOW - 29 * 60, "event": "asked", "speaker": "A", "item": "x"}]
    assert mc.room_ready(log_rows=asked, now=NOW, last_speech_ts=NOW - 300) is False


def test_item_askable():
    assert mc.item_askable("張雨生音樂", {"張雨生"}, {}) is True
    assert mc.item_askable("魔物獵人", set(), {"魔物獵人": "game"}) is True
    assert mc.item_askable("雞排", set(), {"雞排": "food"}) is True
    assert mc.item_askable("欣賞異性", set(), {"欣賞異性": "other"}) is False
    assert mc.item_askable("開燃油車", set(), {}) is False


def test_parse_categories():
    items = ["魔物獵人", "雞排", "欣賞異性"]
    raw = '{"魔物獵人": "game", "雞排": "food", "欣賞異性": "weird", "外來項": "music"}'
    assert mc.parse_categories(raw, items) == {"魔物獵人": "game", "雞排": "food", "欣賞異性": "other"}
    assert mc.parse_categories("not json", items) == {}
    assert mc.parse_categories(None, items) == {}
    assert mc.parse_categories('["魔物獵人"]', items) == {}


def test_items_needing_category():
    tastes = {
        "A": {"張雨生": _taste(), "魔物獵人": _taste(), "雞排": _taste(), "低分": _taste(score=2.9)},
        "B": {"魔物獵人": _taste(), "清酒": _taste()},
    }
    got = mc.items_needing_category(["A", "B"], tastes, {"張雨生"}, {"雞排": "food"})
    assert got == ["魔物獵人", "清酒"]


def _asked(speaker, ago, item="x"):
    return {"ts": NOW - ago, "event": "asked", "speaker": speaker, "item": item}


def test_pick_question_rotates_to_never_asked_person():
    tastes = {"A": {"張雨生": _taste(last_update=NOW - 9000)}, "B": {"周杰倫": _taste(last_update=NOW - 10)}}
    got = mc.pick_question(present=["A", "B"], tastes=tastes, taboos={}, artists=ARTISTS, categories={},
                           log_rows=[_asked("A", 25 * 3600)], now=NOW, last_speech_ts=NOW - 300)
    assert got == ("B", "周杰倫")


def test_pick_question_rotates_to_least_recently_asked_person():
    tastes = {"A": {"張雨生": _taste(last_update=NOW - 9000)}, "B": {"周杰倫": _taste(last_update=NOW - 10)}}
    log = [_asked("A", 26 * 3600), _asked("B", 48 * 3600)]
    got = mc.pick_question(present=["A", "B"], tastes=tastes, taboos={}, artists=ARTISTS, categories={},
                           log_rows=log, now=NOW, last_speech_ts=NOW - 300)
    assert got == ("B", "周杰倫")


def test_pick_question_within_person_picks_oldest_item():
    tastes = {"B": {"周杰倫": _taste(last_update=NOW - 10), "張雨生": _taste(last_update=NOW - 500)}}
    got = mc.pick_question(present=["B"], tastes=tastes, taboos={}, artists=ARTISTS, categories={},
                           log_rows=[], now=NOW, last_speech_ts=NOW - 300)
    assert got == ("B", "張雨生")


def test_pick_question_asks_game_but_not_other():
    tastes = {"A": {"魔物獵人": _taste(last_update=NOW - 10), "欣賞異性": _taste(last_update=NOW - 9000)}}
    cats = {"魔物獵人": "game", "欣賞異性": "other"}
    got = mc.pick_question(present=["A"], tastes=tastes, taboos={}, artists=set(), categories=cats,
                           log_rows=[], now=NOW, last_speech_ts=NOW - 300)
    assert got == ("A", "魔物獵人")

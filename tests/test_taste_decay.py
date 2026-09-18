"""
任務 F：口味時間衰減。30 天沒被強化的口味每週往 0 靠 1 分，|score| < 0.5 刪除。
"""
from __future__ import annotations

import importlib
import inspect
import sys
from pathlib import Path

from suki_memory import apply_taste_decay, TASTE_DECAY_GRACE_S, TASTE_DECAY_DROP_BELOW

DAY = 86400
NOW = 2_000_000_000.0  # 固定大數，避免用 time.time()


def _import_daily_log():
    mod_name = "scripts.analyze_daily_log"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    base = Path(__file__).parent.parent
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    return importlib.import_module(mod_name)


# ── F1: apply_taste_decay ───────────────────────────────────────────────────

def test_within_grace_no_decay():
    taste = {"拉麵": {"score": 5.0, "last_update": NOW - 29 * DAY}}
    changed = apply_taste_decay(taste, NOW)
    assert changed is False
    assert taste["拉麵"]["score"] == 5.0


def test_past_grace_one_week_decays_positive():
    taste = {"拉麵": {"score": 10.0, "last_update": NOW - (30 + 7) * DAY}}
    changed = apply_taste_decay(taste, NOW)
    assert changed is True
    assert taste["拉麵"]["score"] == 9.0


def test_past_grace_one_week_decays_negative_toward_zero():
    taste = {"香菜": {"score": -6.0, "last_update": NOW - (30 + 7) * DAY}}
    apply_taste_decay(taste, NOW)
    assert taste["香菜"]["score"] == -5.0


def test_same_now_called_twice_no_double_decay():
    taste = {"拉麵": {"score": 10.0, "last_update": NOW - (30 + 7) * DAY}}
    apply_taste_decay(taste, NOW)
    assert taste["拉麵"]["score"] == 9.0
    changed2 = apply_taste_decay(taste, NOW)
    assert changed2 is False
    assert taste["拉麵"]["score"] == 9.0


def test_missed_run_catches_up_by_elapsed_weeks():
    taste = {"拉麵": {"score": 10.0, "last_update": NOW - (30 + 14) * DAY}}
    apply_taste_decay(taste, NOW)
    assert taste["拉麵"]["score"] == 8.0


def test_two_separate_runs_no_duplicate_decay():
    taste = {"拉麵": {"score": 10.0, "last_update": NOW - (30 + 14) * DAY}}
    now1 = NOW - 7 * DAY  # 相當於 30+7 天前強化
    apply_taste_decay(taste, now1)
    assert taste["拉麵"]["score"] == 9.0
    apply_taste_decay(taste, NOW)  # 再過一週
    assert taste["拉麵"]["score"] == 8.0


def test_crosses_zero_stops_at_zero_and_dropped():
    taste = {"貓": {"score": 0.6, "last_update": NOW - (30 + 7) * DAY}}
    apply_taste_decay(taste, NOW)
    assert "貓" not in taste


def test_small_score_dropped_below_threshold():
    taste = {"貓": {"score": 0.8, "last_update": NOW - (30 + 7) * DAY}}
    apply_taste_decay(taste, NOW)
    assert "貓" not in taste


def test_no_last_update_sets_decay_anchor_and_no_immediate_decay():
    taste = {"拉麵": {"score": 5.0, "last_update": 0}}
    changed = apply_taste_decay(taste, NOW)
    assert changed is True
    assert taste["拉麵"]["score"] == 5.0
    assert taste["拉麵"]["decay_anchor"] == NOW

    later = NOW + 37 * DAY
    apply_taste_decay(taste, later)
    assert taste["拉麵"]["score"] == 4.0


def test_last_update_unchanged_after_decay():
    lu = NOW - (30 + 7) * DAY
    taste = {"拉麵": {"score": 10.0, "last_update": lu}}
    apply_taste_decay(taste, NOW)
    assert taste["拉麵"]["last_update"] == lu


def test_non_dict_value_preserved():
    taste = {"拉麵": True, "貓": "x"}
    changed = apply_taste_decay(taste, NOW)
    assert changed is False
    assert taste == {"拉麵": True, "貓": "x"}


def test_recent_reinforcement_resets_grace_despite_old_last_decay():
    taste = {
        "拉麵": {
            "score": 5.0,
            "last_update": NOW - 1 * DAY,
            "last_decay": NOW - 100 * DAY,
        }
    }
    changed = apply_taste_decay(taste, NOW)
    assert changed is False
    assert taste["拉麵"]["score"] == 5.0


# ── F2: apply_decay_to_players ──────────────────────────────────────────────

def test_apply_decay_to_players_only_returns_changed():
    mod = _import_daily_log()
    players = {
        "小明": {"taste": {"拉麵": {"score": 3.5, "last_update": NOW - (30 + 7) * DAY}}},
        "小華": {"taste": {"貓": {"score": 5.0, "last_update": NOW - 5 * DAY}}},
    }
    changed = mod.apply_decay_to_players(players, NOW)
    assert changed == ["小明"]
    # score 3.5 -> 2.5，低於 LIKE_THRESHOLD(3.0)，likes 重算後不再含該項
    assert "拉麵" not in players["小明"]["likes"]
    assert players["小明"]["taste"]["拉麵"]["score"] == 2.5


def test_apply_decay_to_players_skips_pseudo_and_bad_values():
    mod = _import_daily_log()
    players = {
        "Marvin推薦（為某玩家）": {"taste": {"拉麵": {"score": 3.5, "last_update": NOW - (30 + 7) * DAY}}},
        "壞資料": True,
        "無taste": {"likes": []},
    }
    changed = mod.apply_decay_to_players(players, NOW)
    assert changed == []


def test_main_wires_decay_after_persist_from_db():
    """衰減要在 LLM 寫回之後、從 DB 讀當下紀錄做——不能套在開頭的 JSON 快照上。"""
    mod = _import_daily_log()
    src = inspect.getsource(mod.main)
    assert "apply_decay_to_players(merged_players" not in src
    assert src.index("decay_players_in_db(") > src.index("persist_players_to_db(")


def test_decay_players_in_db_reads_fresh_db_and_keeps_last_interacted(tmp_path):
    from suki_memory import MemoryManager
    mod = _import_daily_log()
    db, js = str(tmp_path / "m.db"), str(tmp_path / "m.json")
    seed = MemoryManager(db_path=db, json_compat_path=js)
    seed.replace_player_memory("大肚", {
        "taste": {"雞排": {"score": 3.5, "mentions": 1, "first_seen": 1.0,
                          "last_update": NOW - (30 + 7) * DAY}},
        "last_interacted_time": 123.0,
    })
    seed.replace_player_memory("阿銓", {"taste": {"新歡": {"score": 5.0, "mentions": 1,
                                                        "first_seen": 1.0, "last_update": NOW - DAY}}})
    changed = mod.decay_players_in_db(db_path=db, json_path=js, now=NOW)
    assert changed == ["大肚"]
    fresh = MemoryManager(db_path=db, json_compat_path=js)
    p = fresh._cache["大肚"]
    assert p["taste"]["雞排"]["score"] == 2.5
    assert "雞排" not in p["likes"]
    assert p["last_interacted_time"] == 123.0
    assert fresh._cache["阿銓"]["taste"]["新歡"]["score"] == 5.0


# ── F3: prompt 同義詞沿用 ────────────────────────────────────────────────────

def test_prompt_requires_reusing_existing_taste_wording():
    mod = _import_daily_log()
    assert "必須原封不動沿用現有項目的字串" in mod.SYSTEM_PROMPT

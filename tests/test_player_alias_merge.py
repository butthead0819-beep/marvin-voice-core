"""TDD: 把 alias player 合併到 canonical player（suki + music_memory）。

情境（2026-05-26）: 「狗與鹿」與「狗與露」是同一人，stt 漂移造成兩個 player record
各自累積。需要把鹿 merge 進露，整個系統用 canonical name。

Pure 核心：
  - suki_memory.merge_player_records(target, source) → 合併後 dict
  - music_memory.rename_user_in_songs(songs, old, new) → in-place

Rule：
  - target 既有非空欄位保留；source 補 target 缺的欄位
  - list union（dedup by repr）
  - dict 遞迴 merge
  - last_interacted_time 取 max
  - name 欄永遠取 target（不能被 source 蓋）
"""
from __future__ import annotations

from suki_memory import merge_player_records
from music_memory import rename_user_in_songs


# ── suki_memory merge_player_records ─────────────────────────────────────────

def test_target_existing_non_empty_field_wins():
    """target 有值 → 不被 source 蓋。"""
    target = {"suki_impression": "新版 impression"}
    source = {"suki_impression": "舊版 impression"}
    merged = merge_player_records(target, source)
    assert merged["suki_impression"] == "新版 impression"


def test_source_fills_missing_target_field():
    """target 沒這欄位 → 從 source 補。"""
    target = {"suki_impression": "x"}
    source = {"speech_dna": {"pause_proxies": ["uh", "嗯"]}}
    merged = merge_player_records(target, source)
    assert merged["speech_dna"] == {"pause_proxies": ["uh", "嗯"]}


def test_none_treated_as_missing():
    """target 是 None → source 補。"""
    target = {"taboos": None}
    source = {"taboos": ["話題A"]}
    merged = merge_player_records(target, source)
    assert merged["taboos"] == ["話題A"]


def test_empty_string_treated_as_missing():
    target = {"suki_impression": ""}
    source = {"suki_impression": "從舊紀錄補"}
    merged = merge_player_records(target, source)
    assert merged["suki_impression"] == "從舊紀錄補"


def test_lists_union_dedup_by_repr():
    """list 欄位 union；target 順序在前。"""
    target = {"likes": ["音樂", "貓"]}
    source = {"likes": ["音樂", "夜晚"]}
    merged = merge_player_records(target, source)
    assert merged["likes"] == ["音樂", "貓", "夜晚"]


def test_lists_of_dicts_dedup_by_repr():
    """song_history 之類 list[dict] 也要 dedup。"""
    s1 = {"title": "晴天", "ts": 1.0}
    s2 = {"title": "晴天", "ts": 1.0}  # 重複
    s3 = {"title": "稻香", "ts": 2.0}
    target = {"song_history": [s1]}
    source = {"song_history": [s2, s3]}
    merged = merge_player_records(target, source)
    assert merged["song_history"] == [s1, s3]


def test_dicts_recursively_merge():
    target = {"personal_info": {"age": 30}}
    source = {"personal_info": {"job": "engineer", "age": 25}}
    merged = merge_player_records(target, source)
    assert merged["personal_info"] == {"age": 30, "job": "engineer"}  # age target wins


def test_last_interacted_time_takes_max():
    """last_interacted_time 特殊：取 max，不論誰是 target。"""
    target = {"last_interacted_time": 100}
    source = {"last_interacted_time": 200}
    merged = merge_player_records(target, source)
    assert merged["last_interacted_time"] == 200

    # 反向：target 比較新
    target2 = {"last_interacted_time": 999}
    source2 = {"last_interacted_time": 100}
    merged2 = merge_player_records(target2, source2)
    assert merged2["last_interacted_time"] == 999


def test_name_field_always_target_wins():
    """name 欄是 canonical key，不能被 source（舊 alias）蓋。"""
    target = {"name": "狗與露"}
    source = {"name": "狗與鹿"}
    merged = merge_player_records(target, source)
    assert merged["name"] == "狗與露"


def test_target_keys_not_in_source_preserved():
    target = {"a": 1, "b": 2}
    source = {"c": 3}
    merged = merge_player_records(target, source)
    assert merged == {"a": 1, "b": 2, "c": 3}


def test_idempotent():
    """merge(merged, source) == merge(target, source)：再合一次不改變。"""
    target = {"likes": ["a", "b"], "stats": {"plays": 5}}
    source = {"likes": ["b", "c"], "stats": {"plays": 3, "wins": 1}}
    once = merge_player_records(target, source)
    twice = merge_player_records(once, source)
    assert once == twice


# ── music_memory rename_user_in_songs ────────────────────────────────────────

def _song(**overrides):
    base = {
        "title": "x", "uploader": "y",
        "total_plays": 0, "plays": [],
        "requesters": {}, "reactions": {}, "connections": [],
    }
    base.update(overrides)
    return base


def test_rename_user_in_requesters_simple():
    songs = {"k": _song(requesters={"狗與鹿": 3, "Alice": 1})}
    rename_user_in_songs(songs, "狗與鹿", "狗與露")
    assert songs["k"]["requesters"] == {"狗與露": 3, "Alice": 1}


def test_rename_user_sums_when_both_present():
    """同首歌既有 source 也有 target 的點播次數 → 加總。"""
    songs = {"k": _song(requesters={"狗與鹿": 2, "狗與露": 5})}
    rename_user_in_songs(songs, "狗與鹿", "狗與露")
    assert songs["k"]["requesters"] == {"狗與露": 7}


def test_rename_user_in_plays_by_field():
    songs = {"k": _song(plays=[{"by": "狗與鹿", "ts": 1.0}, {"by": "Alice", "ts": 2.0}])}
    rename_user_in_songs(songs, "狗與鹿", "狗與露")
    assert songs["k"]["plays"] == [{"by": "狗與露", "ts": 1.0}, {"by": "Alice", "ts": 2.0}]


def test_rename_user_in_connections_dedup():
    songs = {"k": _song(connections=["狗與鹿", "狗與露", "Alice"])}
    rename_user_in_songs(songs, "狗與鹿", "狗與露")
    assert songs["k"]["connections"] == ["狗與露", "Alice"]


def test_rename_user_in_reactions_merges_per_user_dict():
    songs = {"k": _song(reactions={
        "狗與鹿": {"feelings": ["懷舊"], "quotes": ["A"]},
        "狗與露": {"feelings": ["流淚"], "quotes": ["B"]},
    })}
    rename_user_in_songs(songs, "狗與鹿", "狗與露")
    rx = songs["k"]["reactions"]
    assert "狗與鹿" not in rx
    assert set(rx["狗與露"]["feelings"]) == {"懷舊", "流淚"}
    assert set(rx["狗與露"]["quotes"]) == {"A", "B"}


def test_rename_user_no_op_when_old_absent():
    songs = {"k": _song(requesters={"Alice": 1})}
    rename_user_in_songs(songs, "狗與鹿", "狗與露")
    assert songs["k"]["requesters"] == {"Alice": 1}

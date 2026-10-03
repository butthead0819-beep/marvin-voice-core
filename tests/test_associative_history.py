"""Unit tests for associative_history.py — associative curation 30 天選曲紀錄。"""
import json
import time

import associative_history
from associative_history import append_pick, is_repeat, load_recent_picks, PROMPT_MAX_PICKS


def test_append_and_load_newest_first(tmp_path):
    path = tmp_path / "picks.jsonl"
    now = time.time()
    append_pick("阿杜", "他一定很愛你", now - 100, path=path)
    append_pick("美秀集團", "手機錢包鑰匙菸", now - 50, path=path)

    picks = load_recent_picks(now, path=path)
    assert picks == ["美秀集團 - 手機錢包鑰匙菸", "阿杜 - 他一定很愛你"]


def test_load_excludes_older_than_30_days(tmp_path):
    path = tmp_path / "picks.jsonl"
    now = time.time()
    append_pick("阿杜", "他一定很愛你", now - 31 * 86400, path=path)
    append_pick("美秀集團", "手機錢包鑰匙菸", now - 5 * 86400, path=path)

    picks = load_recent_picks(now, path=path)
    assert picks == ["美秀集團 - 手機錢包鑰匙菸"]


def test_load_dedupes_same_song_keeps_latest(tmp_path):
    path = tmp_path / "picks.jsonl"
    now = time.time()
    # 確認 _VARIANT_RE 真的會去掉 "(Live)" 這種後綴
    from music_recommender import normalize_title
    assert normalize_title("他一定很愛你 (Live)") == normalize_title("他一定很愛你")

    append_pick("阿杜", "他一定很愛你", now - 200, path=path)
    append_pick("阿杜", "他一定很愛你 (Live)", now - 100, path=path)

    picks = load_recent_picks(now, path=path)
    assert picks == ["阿杜 - 他一定很愛你 (Live)"]


def test_load_missing_file_returns_empty(tmp_path):
    path = tmp_path / "does_not_exist.jsonl"
    assert load_recent_picks(time.time(), path=path) == []


def test_load_skips_bad_lines(tmp_path):
    path = tmp_path / "picks.jsonl"
    now = time.time()
    with path.open("w", encoding="utf-8") as f:
        f.write("not json\n")
        f.write(json.dumps({"ts": now - 10, "artist": "阿杜", "song": "他一定很愛你"}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"artist": "缺ts"}, ensure_ascii=False) + "\n")

    picks = load_recent_picks(now, path=path)
    assert picks == ["阿杜 - 他一定很愛你"]


def test_load_caps_at_prompt_max_picks(tmp_path):
    path = tmp_path / "picks.jsonl"
    now = time.time()
    for i in range(PROMPT_MAX_PICKS + 20):
        append_pick(f"歌手{i}", f"歌曲{i}", now - i, path=path)

    picks = load_recent_picks(now, path=path)
    assert len(picks) == PROMPT_MAX_PICKS


def test_is_repeat_hit_and_miss():
    recent = ["阿杜 - 他一定很愛你", "美秀集團 - 手機錢包鑰匙菸"]
    assert is_repeat("他一定很愛你", recent) is True
    assert is_repeat("他一定很愛你 (Live)", recent) is True
    assert is_repeat("浪子回頭", recent) is False


def test_is_repeat_handles_entry_without_separator():
    recent = ["他一定很愛你"]
    assert is_repeat("他一定很愛你", recent) is True
    assert is_repeat("浪子回頭", recent) is False


def test_load_recent_picks_uses_module_history_path_when_no_path(tmp_path, monkeypatch):
    monkeypatch.setattr(associative_history, "HISTORY_PATH", tmp_path / "patched.jsonl")
    now = time.time()
    append_pick("阿杜", "他一定很愛你", now - 10)

    picks = load_recent_picks(now)
    assert picks == ["阿杜 - 他一定很愛你"]

"""game_aliases — 遊戲名誤聽對照表（design 2026-10-23）。見 game_aliases.py docstring。"""
from __future__ import annotations

from pathlib import Path

import pytest

from game_aliases import alias_hint, load_aliases, match_games

_DATA_PATH = Path(__file__).resolve().parent.parent / "data/game_aliases.json"


def _real_entries() -> list[dict]:
    return load_aliases(_DATA_PATH)


# ── 真資料：命中 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "我想要查 Grandetwo 在涼亭區域的詞別廳可以做什麼",
    "那你知道 Gronde two這個遊戲嗎",
    "馬文你知道 Gronede2這個遊戲嗎",
    "我你知道 Grounde too這個遊戲嗎",
    "哎馬文幫我查 groundeto的綠背春象去哪裏找",
    "馬文幫我查 Grandy two遊戲裡麵的水下武器要怎麼製作",
    "然後 gronder two 也會更新啊",
    "I gunder two",
    "好你知道 Brotoo這個遊戲嗎",
    "幫我查光點兔的三級鏟子材料去哪裡拿",
    "南文幫我查光烈兔的綠春綠被春象還是綠被裝向二代在哪裡找",
    "幫我查詢光點二代",
    "哎馬文幫我查「圓地豆」的綠背春象去哪裏找",
    "馬文查詢韋小世界生子名戰對我這個遊戲",
    "圍剿世界生存戰二怎麼查不到",
    "馬文你知道幫註那種戲嗎",
    "微小世界生存戰2 的溫室區怎麼過去",
])
def test_match_games_hits_grounded_2(text):
    matches = match_games(text, _real_entries())
    assert matches, f"{text!r} 應命中"
    assert all(entry["name"] == "Grounded 2" for _, entry in matches)


# ── 真資料：不命中 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "可以啦原地跳迴來就可以了",
    "然後屍體有涼亭光點",
    "他就就不用因為光電機已經被淘汰了",
    "Grounding",
    "最近的臺風是什麼強度什麼時候登陸臺灣",
    "藍牙 EDR就是代表有 A twoDP 的意思嗎",
    "Grounded 2 的溫室區怎麼過去",
    "",
])
def test_match_games_no_false_positive(text):
    assert match_games(text, _real_entries()) == []


# ── 正規化 ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["GRONDE   TWO", "gronde-two"])
def test_match_games_normalization(text):
    matches = match_games(text, _real_entries())
    assert matches
    assert matches[0][1]["name"] == "Grounded 2"


# ── 由長到短 ──────────────────────────────────────────────────────────────────

def test_match_games_prefers_longest_candidate():
    entries = [{"name": "G", "zh": "", "aka": [], "heard": ["光點", "光點二代"]}]
    matches = match_games("光點二代", entries)
    assert matches == [("光點二代", entries[0])]


# ── 同一個 entry 只回一次 ──────────────────────────────────────────────────────

def test_match_games_dedupes_per_entry():
    matches = match_games("圓地豆跟幫註是一樣的遊戲", _real_entries())
    assert len(matches) == 1


# ── load_aliases 容錯 ─────────────────────────────────────────────────────────

def test_load_aliases_missing_path_returns_empty(tmp_path):
    assert load_aliases(tmp_path / "nope.json") == []


def test_load_aliases_bad_json_returns_empty(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not json", encoding="utf-8")
    assert load_aliases(p) == []


def test_load_aliases_wrong_shape_returns_empty(tmp_path):
    p = tmp_path / "wrong.json"
    p.write_text('{"games": "x"}', encoding="utf-8")
    assert load_aliases(p) == []


# ── alias_hint ────────────────────────────────────────────────────────────────

def test_alias_hint_empty_matches_returns_empty_string():
    assert alias_hint([]) == ""


def test_alias_hint_real_match_exact_text():
    matches = match_games("圓地豆", _real_entries())
    assert alias_hint(matches) == (
        "【語音辨識常把遊戲名聽糊：「圓地豆」＝遊戲《Grounded 2》（微小世界生存戰2）。"
        "回答時以這款遊戲為準，不要猜成別款遊戲】"
    )


def test_alias_hint_no_zh_has_no_parentheses():
    entry = {"name": "G", "zh": "", "aka": [], "heard": ["gee"]}
    matches = match_games("gee", [entry])
    out = alias_hint(matches)
    assert "（" not in out
    assert out == "【語音辨識常把遊戲名聽糊：「gee」＝遊戲《G》。回答時以這款遊戲為準，不要猜成別款遊戲】"

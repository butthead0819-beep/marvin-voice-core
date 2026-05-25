"""TDD — lyrics_seek：LRC 解析 + 在 LRC 內找 fragment 對應的時間戳。

支援「找歌詞 X」找到歌之後，定位 X 在 LRC 哪個時間點。Pure functions，無 I/O。

LRC 格式：每行 [mm:ss.xx]歌詞文字；可有 [ti:...] [ar:...] 等 metadata（要略過）。
"""
from __future__ import annotations

import pytest

from intent_agents.lyrics_seek import parse_lrc, find_lyrics_timestamp


# ── LRC fixture：模擬青花瓷主歌一段 ──────────────────────────────────────────────

LRC_QINGHUACI = """\
[ti:青花瓷]
[ar:周杰倫]
[00:00.00]青花瓷 - 周杰倫
[00:12.34]素胚勾勒出青花筆鋒濃轉淡
[00:18.50]瓶身描繪的牡丹一如妳初妝
[00:25.10]冉冉檀香透過窗心事我了然
[00:31.80]宣紙上走筆至此擱一半
[01:23.45]天青色等煙雨 而我在等妳
[01:30.20]炊煙裊裊昇起 隔江千萬里
[01:36.90]在瓶底書漢隸仿前朝的飄逸
[02:50.10]天青色等煙雨 而我在等妳
"""


# ── parse_lrc ────────────────────────────────────────────────────────────────

def test_parse_lrc_returns_list_of_timestamp_text_tuples():
    lines = parse_lrc(LRC_QINGHUACI)
    assert isinstance(lines, list)
    assert all(isinstance(t, float) and isinstance(s, str) for t, s in lines)


def test_parse_lrc_skips_metadata_lines():
    lines = parse_lrc(LRC_QINGHUACI)
    texts = [s for _, s in lines]
    # [ti:] [ar:] 不該出現在結果裡
    assert not any("ti:" in t or "ar:" in t for t in texts)


def test_parse_lrc_correct_timestamp_seconds():
    """[01:23.45] 應該 parse 為 83.45 秒。"""
    lines = parse_lrc(LRC_QINGHUACI)
    target = next((t for t, s in lines if "天青色等煙雨" in s), None)
    assert target is not None
    assert target == pytest.approx(83.45)


def test_parse_lrc_handles_no_fraction():
    """[01:23] 沒小數部分也要能 parse → 83.0 秒。"""
    lrc = "[01:23]整數時間戳測試"
    lines = parse_lrc(lrc)
    assert len(lines) == 1
    assert lines[0][0] == pytest.approx(83.0)


def test_parse_lrc_handles_empty_input():
    assert parse_lrc("") == []


def test_parse_lrc_handles_no_timestamp_lines():
    """純文字（沒 timestamp 標記）→ 空 list。"""
    assert parse_lrc("這是純歌詞\n沒有時間戳") == []


def test_parse_lrc_strips_line_whitespace():
    lrc = "[00:10.00]   有前後空白的歌詞   "
    lines = parse_lrc(lrc)
    assert lines[0][1] == "有前後空白的歌詞"


# ── find_lyrics_timestamp ────────────────────────────────────────────────────

def test_finds_exact_substring_match():
    result = find_lyrics_timestamp(LRC_QINGHUACI, "天青色等煙雨")
    assert result is not None
    ts, line = result
    assert ts == pytest.approx(83.45)
    assert "天青色等煙雨" in line


def test_returns_first_match_when_multiple():
    """fragment 在 01:23 跟 02:50 都出現，應該取第一次（01:23）。"""
    result = find_lyrics_timestamp(LRC_QINGHUACI, "天青色等煙雨")
    assert result is not None
    ts, _ = result
    assert ts == pytest.approx(83.45)  # 不是 170.10


def test_partial_match_within_longer_line():
    """fragment 只是 LRC 某行的一部分，也要找到。"""
    result = find_lyrics_timestamp(LRC_QINGHUACI, "炊煙裊裊")
    assert result is not None
    ts, line = result
    assert ts == pytest.approx(90.20)
    assert "炊煙裊裊" in line


def test_returns_none_when_fragment_not_in_lrc():
    result = find_lyrics_timestamp(LRC_QINGHUACI, "完全不存在的字串 xyz")
    assert result is None


def test_returns_none_for_empty_fragment():
    assert find_lyrics_timestamp(LRC_QINGHUACI, "") is None


def test_returns_none_for_whitespace_fragment():
    assert find_lyrics_timestamp(LRC_QINGHUACI, "   ") is None


def test_returns_none_for_empty_lrc():
    assert find_lyrics_timestamp("", "天青色等煙雨") is None


def test_returns_none_for_lrc_without_timestamps():
    assert find_lyrics_timestamp("純文字無時間戳", "純文字") is None


def test_whitespace_in_fragment_normalized():
    """fragment 帶空白也要能匹配（STT 偶爾會插空白）。"""
    result = find_lyrics_timestamp(LRC_QINGHUACI, "天青色 等煙雨")
    assert result is not None
    ts, _ = result
    assert ts == pytest.approx(83.45)


def test_fragment_longer_than_any_line():
    """fragment 跨多行（連續 LRC 行的歌詞拼接）→ 找第一行所在 timestamp。"""
    # 「天青色等煙雨 而我在等妳 炊煙裊裊昇起」橫跨兩行，第一行起點是 01:23.45
    result = find_lyrics_timestamp(LRC_QINGHUACI, "天青色等煙雨而我在等妳炊煙裊裊")
    # 這個複雜 case：MVP 不一定要支援，但若實作了應該回第一行
    # 接受兩種行為：None（MVP 不做跨行） 或 (83.45, 第一行)
    if result is not None:
        ts, _ = result
        assert ts == pytest.approx(83.45)


# ── 真實 STT 場景 fixture ─────────────────────────────────────────────────────

def test_typical_user_query_finds_chorus():
    """模擬使用者實際說的歌詞 → 命中副歌時間點。"""
    result = find_lyrics_timestamp(LRC_QINGHUACI, "天青色等煙雨")
    assert result is not None
    ts, line = result
    # 落在合理範圍：副歌前 1 分鐘左右
    assert 60 < ts < 120

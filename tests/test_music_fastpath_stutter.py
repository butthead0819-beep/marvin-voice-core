"""TDD: STT 口吃疊字塌縮——fastpath 免 LLM 救回（2026-07-04 使用者點題）。

實案（7/4 09:48）：「播放播放陳華的播放陳華的左邊的人」——STT 漸進式口吃
把片段疊接，中段殘留「播放」+重複片段令 _title_covered 覆蓋率崩 → fastpath
miss → 白送 2s cleaner LLM。重複片段是機械模式，正則塌縮即可，
符合漏斗哲學：便宜關卡能救的不送貴的。
"""
from __future__ import annotations

from music_fastpath import collapse_stutter


def test_collapse_progressive_stutter_real_case():
    # 7/4 09:48 實案。2 字的「播放播放」由 strip_command_prefix 的 (播放)+ 剝除
    # ——塌縮只負責 ≥3 字片段，兩關合成才是管線真效果
    from music_fastpath import strip_command_prefix
    out = strip_command_prefix(collapse_stutter("播放播放陳華的播放陳華的左邊的人"))
    assert out == "陳華的左邊的人"


def test_collapse_doubled_command():
    from music_fastpath import strip_command_prefix
    assert strip_command_prefix(collapse_stutter("播放播放晴天")) == "晴天"


def test_collapse_doubled_wake_phrase():
    # 7/3 21:38 型：「馬文播放X 馬文播放X」debounce 疊接
    assert collapse_stutter("馬文播放陶喆的愛很簡單 馬文播放陶喆的愛很簡單") == "馬文播放陶喆的愛很簡單"


def test_clean_text_unchanged():
    assert collapse_stutter("播放陳華的左邊的人") == "播放陳華的左邊的人"


def test_legit_repetition_in_title_preserved():
    # 歌名本身含疊字（如「對面的女孩看過來看過來」型）——只塌「長片段」重複，
    # 2 字內的疊詞不動（好好、天天、看過來看過來是 3 字組重複會塌…取捨：
    # 塌縮閾值 ≥3 字片段，歌名 2 字疊詞（好好想想）安全）
    assert collapse_stutter("好好") == "好好"
    assert collapse_stutter("天天想你") == "天天想你"


def test_empty_safe():
    assert collapse_stutter("") == ""


def test_match_rescues_stuttered_query_end_to_end(tmp_path):
    """整合：口吃句直接進 match() 就命中——不再依賴 cleaner LLM。"""
    import json
    from music_fastpath import MusicFastPath
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps([{"name": "陳華 左邊的人", "videoId": "tER-0RhdAow"}],
                                  ensure_ascii=False), encoding="utf-8")
    fp = MusicFastPath(catalog_path=catalog)
    hit = fp.match("播放播放陳華的播放陳華的左邊的人")   # 7/4 09:48 原句
    assert hit is not None
    assert hit[0] == "陳華 左邊的人"

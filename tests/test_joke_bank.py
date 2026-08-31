"""joke_bank.JokeBank：歌名拼音 → 冷笑話 的比對。"""
from __future__ import annotations

import textwrap

import pytest

pytest.importorskip("yaml")
pytest.importorskip("pypinyin")

from joke_bank import JokeBank


@pytest.fixture
def bank(tmp_path):
    p = tmp_path / "jb.yaml"
    p.write_text(textwrap.dedent("""\
        - joke: "告白氣球的笑話。……唉。"
          hooks: [告白氣球, 告白, 氣球]
        - joke: "稻草人站在田裡。……付出跟頭銜不成正比。"
          hooks: [稻香, 稻草]
        - joke: "月亮每天換形狀。……標準不對所有人開放。"
          hooks: [月亮, 月光]
        - joke: "單字 hook 不該生效。"
          hooks: [愛]
    """), encoding="utf-8")
    sj = tmp_path / "sj.yaml"
    sj.write_text(textwrap.dedent("""\
        - key: VID_CONFESSION
          title: 周杰倫 告白氣球
          style: puns
          joke: "這首歌的專屬笑話。……宇宙也沒回音。"
        - key: VID_SKIPPED
          title: 某首生不出的歌
          style: skip
          joke: ""
    """), encoding="utf-8")
    return JokeBank(p, sj)


def test_video_id_specific_joke_wins_over_pinyin(bank):
    # 有 videoId 專屬笑話 → 用它，不走泛用 hook 庫
    assert bank.match("周杰倫 告白氣球", video_id="VID_CONFESSION").startswith("這首歌的專屬笑話")


def test_video_id_miss_falls_through_to_pinyin(bank):
    # videoId 沒登記 → 退回歌名拼音比對
    assert bank.match("周杰倫 告白氣球", video_id="VID_UNKNOWN").startswith("告白氣球的笑話")


def test_skip_style_song_joke_ignored(bank):
    assert bank.match("某首歌", video_id="VID_SKIPPED") is None


def test_video_id_joke_respects_exclude(bank):
    jk = bank.match("x", video_id="VID_CONFESSION")
    # 被 exclude → 不回這則；歌名也不匹配任何 hook → None
    assert bank.match("x", video_id="VID_CONFESSION", exclude={jk}) is None


def test_exact_title_hits(bank):
    assert bank.match("周杰倫 告白氣球").startswith("告白氣球的笑話")


def test_pinyin_homophone_hits(bank):
    # 「稻香」dao xiang；hook「稻香」= dao xiang → 命中
    assert "稻草人" in bank.match("稻香")


def test_substring_syllable_boundary_no_false_positive(bank):
    # 「曹操」cao cao 不該命中「稻草」dao cao（跨音節子串）
    assert bank.match("JJ林俊傑 曹操") is None


def test_single_char_hook_ignored(bank):
    # 「愛」是單字 hook，載入時就被濾掉 → 任何含「愛」的歌名都不會命中它
    assert bank.match("愛你一萬年") is None


def test_no_match_returns_none(bank):
    assert bank.match("完全無關的歌名 XYZ") is None


def test_exclude_skips_recent(bank):
    joke = bank.match("告白氣球")
    assert bank.match("告白氣球", exclude={joke}) is None


def test_empty_or_missing_title(bank):
    assert bank.match("") is None
    assert bank.match(None) is None


def test_broken_yaml_disables_gracefully(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("{{{ not yaml", encoding="utf-8")
    jb = JokeBank(p)
    assert jb.match("告白氣球") is None


def test_missing_file_disables_gracefully(tmp_path):
    jb = JokeBank(tmp_path / "nope.yaml")
    assert jb.match("告白氣球") is None


def test_hot_reload_on_mtime_change(bank, tmp_path):
    assert bank.match("溫柔") is None
    (tmp_path / "jb.yaml").write_text(
        '- joke: "溫柔的人容易受傷。……宇宙輾過最軟的東西。"\n  hooks: [溫柔]\n',
        encoding="utf-8",
    )
    import os, time
    future = time.time() + 10
    os.utime(tmp_path / "jb.yaml", (future, future))
    assert "溫柔的人" in bank.match("五月天 溫柔")

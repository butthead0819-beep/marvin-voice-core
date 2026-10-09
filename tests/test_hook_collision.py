"""hook_collision：聊天撞歌詞比對核心。純函式測試，無 IO、無 asyncio。"""
from __future__ import annotations

from hook_collision import (
    Collision,
    CollisionLedger,
    collision_template,
    filter_consented,
    find_collision,
    utts_since,
    verbatim_ok,
)

LYRICS = "拿走了什么\n我們的世界並不像你說的真有那麼壞\n能不能就陪着我天长地久"


def test_literal_hit_returns_collision():
    c = find_collision([("showay", "我覺得他真的拿走了什麼東西")], LYRICS)
    assert c is not None
    assert c.kind == "literal"
    assert c.speaker == "showay"
    assert c.lyric_line == "拿走了什麼"
    assert c.score >= 4


def test_simplified_lyric_matches_traditional_chat():
    c = find_collision([("showay", "拿走了什麼啊")], "拿走了什么")
    assert c is not None and c.kind == "literal"


def test_generic_phrase_does_not_hit():
    lyrics = "是不是可以的那天\n不知道你心裡還能否"
    assert find_collision([("showay", "我覺得是不是可以再說")], lyrics) is None
    assert find_collision([("showay", "我真的不知道怎麼辦")], lyrics) is None


def test_english_fragment_does_not_hit():
    assert find_collision([("showay", "Meanne is cool")], "mean the world") is None


def test_marvin_speaker_is_skipped():
    assert find_collision([("Marvin", "我覺得他真的拿走了什麼東西")], LYRICS) is None


def test_wake_word_utterance_is_skipped():
    assert find_collision([("showay", "馬文你覺得拿走了什麼")], LYRICS) is None


def test_title_or_artist_utterance_is_skipped():
    assert find_collision([("showay", "這首拿走了什麼好聽")], LYRICS, title="拿走了什麼") is None
    assert find_collision([("showay", "周杰倫拿走了什麼")], LYRICS, artist="周杰倫") is None


def test_sing_along_utterance_is_skipped():
    lyrics = "拿走了什麼東西很重要"
    assert find_collision([("showay", "我就是拿走了什麼東西很重要啦")], lyrics) is None


def test_exclude_blocks_same_quote_and_line():
    utts = [("showay", "我覺得他真的拿走了什麼東西")]
    assert find_collision(utts, LYRICS, exclude={"拿走了什麼"}) is None
    assert find_collision(utts, LYRICS, exclude={"我覺得他真的拿走了什麼東西"}) is None


def test_pinyin_only_homophone_is_pinyin_kind():
    c = find_collision([("showay", "他拿走了神麼東西")], "拿走了什么")
    assert c is not None and c.kind == "pinyin_only"


def test_empty_inputs_return_none():
    assert find_collision([], LYRICS) is None
    assert find_collision([("showay", "拿走了什麼")], "") is None


def test_long_chat_quote_is_windowed_around_match():
    text = "前面這段話很長很長很長很長很長拿走了什麼後面也很長很長很長很長很長"
    c = find_collision([("showay", text)], "拿走了什么")
    assert c is not None
    assert len(c.chat_quote) <= 2 * 4 + len("拿走了什麼")
    assert "拿走了什麼" in c.chat_quote


def test_module_has_no_io_imports():
    import hook_collision
    src = open(hook_collision.__file__, encoding="utf-8").read()
    for bad in ("import requests", "import aiohttp", "import discord", "import sqlite3"):
        assert bad not in src


def _c(speaker="showay", quote="我覺得他真的拿走了什麼東西", line="拿走了什麼"):
    return Collision(speaker=speaker, chat_quote=quote, lyric_line=line, matched_key=line, score=5, kind="literal")


def test_ledger_ready_after_min_gap_songs():
    led = CollisionLedger(min_gap_songs=2)
    led.mark(_c(), now=1000.0)
    assert not led.ready()
    led.tick_song()
    assert not led.ready()
    led.tick_song()
    assert led.ready()


def test_ledger_speaker_cooldown_blocks_same_speaker_30_min():
    led = CollisionLedger(speaker_cooldown_s=1800.0)
    led.mark(_c(), now=1000.0)
    assert led.blocked_speakers(now=2000.0) == {"showay"}
    assert led.blocked_speakers(now=2800.0) == set()


def test_ledger_excludes_used_quote_and_line():
    led = CollisionLedger()
    led.mark(_c(), now=0.0)
    assert "我覺得他真的拿走了什麼東西" in led.exclude()
    assert "拿走了什麼" in led.exclude()


def test_verbatim_ok_requires_both_quote_and_line():
    c = _c()
    assert verbatim_ok("聊天室剛才 showay 說：「我覺得他真的拿走了什麼東西」，這句歌詞有「拿走了什麼」", c)
    assert not verbatim_ok("showay 剛才說了一句話，這首剛好唱到拿走了什麼", c)
    assert not verbatim_ok("我覺得他真的很好聽", c)


def test_verbatim_ok_ignores_punctuation_and_simplified():
    c = _c(quote="我覺得他真的拿走了什麼東西", line="拿走了什麼")
    assert verbatim_ok("「我覺得，他真的拿走了什么東西」拿走了什么！", c)


def test_collision_template_has_no_first_person_and_both_quotes():
    s = collision_template(_c(), title="測試歌")
    assert "我" not in s.replace("我覺得他真的拿走了什麼東西", "")
    assert "我覺得他真的拿走了什麼東西" in s and "拿走了什麼" in s and "《測試歌》" in s


def test_filter_consented_drops_non_consented_before_match():
    utts = [("showay", "甲"), ("Bob", "乙")]
    assert filter_consented(utts, lambda s: s == "showay") == [("showay", "甲")]


def test_utts_since_keeps_only_window():
    entries = [{"timestamp": 10.0, "speaker": "a", "text": "舊"}, {"timestamp": 20.0, "speaker": "b", "text": "新"}]
    assert utts_since(entries, 15.0) == [("b", "新")]

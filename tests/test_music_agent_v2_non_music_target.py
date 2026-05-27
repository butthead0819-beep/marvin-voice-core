"""MusicAgentV2 議題 D — 非音樂類 target 不該命中 weak_play_specific.

5/27 judge outcomes 分析議題 D：L48「麻煩幫我找到這個詭異的線上網站」
被 weak_play_specific 0.95 命中（kw=幫我找, song=線上網站）。

既有 post_match_filter 只擋 weak_play_long_string / artist_only 的 target slot，
**沒擋 weak_play_specific 的 song slot**。本 commit 補上 + 擴 blocklist 加非音樂
名詞後綴（網站 / 影片 / 文章 / 圖片 / 連結 / 新聞 / 資料 / 帳號 / 密碼 / 信件 / 郵件 / 訊息）。
"""
from __future__ import annotations

import pytest

from intent_agents.music_agent_v2 import MusicAgentV2
from intent_bus import IntentContext


class _FakeCtrl:
    _STRONG_PLAY_KW = ["放音樂", "播音樂", "放首歌", "播首歌", "放一首", "播一首",
                       "來首", "搜尋歌曲", "play music", "play song"]
    _WEAK_PLAY_KW = ["播放", "我想聽", "放點", "播點", "幫我找", "幫我放"]
    _MUSIC_SKIP_KW = ["換一首", "下一首", "跳過", "換歌", "不要這首", "skip"]
    _MUSIC_STOP_KW = ["停止播放", "音樂停", "不要播了", "關掉音樂"]
    _MUSIC_PAUSE_KW = ["暫停音樂", "暫停一下", "pause"]
    _MUSIC_RESUME_KW = ["繼續播", "繼續音樂", "播回來", "resume"]
    async def _safe_music_command(self, *a, **kw): pass
    async def _ask_music_followup(self, *a, **kw): pass


def _ctx(query: str) -> IntentContext:
    return IntentContext(
        speaker="x", raw_text=query, query=query, original_raw=query,
        wake_intent=0.9, stream_active=False, game_mode=False,
        is_owner=False, now=0.0, mode="normal",
    )


# ── 議題 D 的原 case（L48）──────────────────────────────────────────────


def test_l48_weak_play_specific_with_website_target_rejected():
    """L48「麻煩幫我找到這個詭異的線上網站」應該不命中 weak_play_specific 0.95。"""
    agent = MusicAgentV2(_FakeCtrl())
    bid = agent.bid(_ctx("麻煩幫我找到這個詭異的線上網站"))
    # 不該是 specific 0.95；若 fall through 到其他 schema 拿低 conf 可接受
    assert bid.confidence < 0.95, f"expected <0.95 (rejected specific), got {bid.confidence} ({bid.reason})"


# ── 各種非音樂名詞後綴 ───────────────────────────────────────────────────


@pytest.mark.parametrize("query", [
    "幫我找這個影片",
    "幫我找這篇文章",
    "幫我找他的圖片",
    "我想聽那邊的連結",
    "播放剛剛的新聞",
    "幫我找剛剛的資料",
    "播放他的帳號",
    "我想聽下載的密碼",
    "幫我找昨天的信件",
    "播放小王的郵件",
    "幫我找最新的訊息",
])
def test_non_music_noun_suffixes_rejected_for_specific(query):
    agent = MusicAgentV2(_FakeCtrl())
    bid = agent.bid(_ctx(query))
    assert bid.confidence < 0.95, f"{query!r} should not pass specific 0.95; got {bid.confidence} ({bid.reason})"


# ── happy path（不能誤殺真音樂 intent）─────────────────────────────────


@pytest.mark.parametrize("query", [
    "播放陶喆的天天",
    "我想聽周杰倫的稻香",
    "幫我找劉德華的練習",
    "播放魏如萱的你呀你呀",
    "播放滅火器的心內話",
])
def test_real_music_intents_still_match_specific(query):
    agent = MusicAgentV2(_FakeCtrl())
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.95, f"{query!r} should still be specific 0.95; got {bid.confidence} ({bid.reason})"
    assert "weak_play_specific" in bid.reason


# ── 邊界：blocklist 詞出現在歌名中段不該被誤殺 ──────────────────────────


def test_blocklist_word_mid_song_does_not_reject():
    """歌名中段含 blocklist 詞（如「網站」）—— suffix match 不該誤殺。
    例：「魏如萱的網站之歌」歌名是「網站之歌」，結尾是「之歌」不在 blocklist → pass。"""
    agent = MusicAgentV2(_FakeCtrl())
    bid = agent.bid(_ctx("播放魏如萱的網站之歌"))
    assert bid.confidence == 0.95

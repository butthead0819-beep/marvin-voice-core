"""TDD — PersonalShuffleAgent：語音觸發「連續隨機播我的歌單」。

宣告式 IntentAgent，雙 lookahead（連續/隨機詞 + 我的歌單/我點過 詞）避免誤觸發。
mode_compatible = {normal, stream}；handler 進 MusicCog.start_personal_shuffle。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_bus import IntentContext
from intent_agents.personal_shuffle_agent import PersonalShuffleAgent


def _ctx(query, *, mode="normal", speaker="阿明"):
    return IntentContext(
        speaker=speaker, raw_text=query, query=query, original_raw=query,
        wake_intent=1.0, stream_active=False, game_mode=False,
        is_owner=False, now=0.0, mode=mode,
    )


def _agent():
    return PersonalShuffleAgent(MagicMock())


# ── mode gate ─────────────────────────────────────────────────────────────

def test_game_mode_dense_zero():
    bid = _agent().bid(_ctx("連續播我的歌單", mode="game"))
    assert bid.confidence == 0.0
    assert bid.reason.startswith("mode_mismatch")


# ── happy path：各種講法都命中 ──────────────────────────────────────────────

def test_continuous_my_playlist_bids():
    a = _agent()
    for q in ["連續播我的歌單", "隨機播我點過的歌", "一直播我的歌", "循環播個人歌單",
              "幫我隨機輪播我之前點的歌"]:
        bid = a.bid(_ctx(q))
        assert bid.confidence >= 0.8, f"應命中: {q}"
        assert bid.name == a.name


# ── negative space：不該誤觸發 ─────────────────────────────────────────────

def test_unrelated_dense_zero():
    a = _agent()
    bid = a.bid(_ctx("今天天氣真好"))
    assert bid.confidence == 0.0
    assert bid.reason == "no_match"


def test_play_someone_elses_song_does_not_match():
    # 「播周杰倫的歌」是一般點歌（MusicAgentV2 的事），不是個人歌單連續播
    a = _agent()
    assert a.bid(_ctx("播周杰倫的歌")).confidence == 0.0
    assert a.bid(_ctx("隨便放首歌")).confidence == 0.0


def test_continuous_word_without_mine_does_not_match():
    # 只有「連續播」沒有「我的歌單/我點過」→ 不命中（避免吃掉一般連播語意）
    assert _agent().bid(_ctx("連續播放音樂")).confidence == 0.0


# ── stop：結束個人歌單、回到一般/主題自動播放 ─────────────────────────────

def test_stop_phrases_bid():
    a = _agent()
    for q in ["停掉我的歌單", "結束個人歌單", "不要再放我的歌單", "換回一般播放",
              "恢復自動推薦", "回到正常播放"]:
        bid = a.bid(_ctx(q))
        assert bid.confidence >= 0.95, f"應命中 stop: {q}"
        assert bid.reason == "personal_shuffle_stop"


def test_stop_beats_generic_music_stop():
    # 指名「歌單」時要贏過 MusicAgentV2.control_stop(0.95)，否則會停掉所有音樂
    assert _agent().bid(_ctx("停掉我的歌單")).confidence > 0.95


def test_generic_stop_alone_not_matched():
    # 純「停 / 停掉音樂」仍交給一般 control_stop（停掉所有音樂），個人 agent 不攔
    a = _agent()
    assert a.bid(_ctx("停")).confidence == 0.0
    assert a.bid(_ctx("停掉音樂")).confidence == 0.0


@pytest.mark.asyncio
async def test_stop_handler_calls_cog_stop():
    ctrl = MagicMock()
    cog = MagicMock()
    cog.stop_personal_shuffle.return_value = True
    cog._vc.return_value = None  # 無頻道 → 跳過 ack
    ctrl.bot.cogs.get.return_value = cog
    a = PersonalShuffleAgent(ctrl)
    bid = a.bid(_ctx("換回一般播放"))
    await bid.handler()
    cog.stop_personal_shuffle.assert_called_once()


# ── handler 整合 ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handler_calls_cog_start_personal_shuffle():
    ctrl = MagicMock()
    cog = MagicMock()
    cog.start_personal_shuffle = AsyncMock(return_value=(True, "ok"))
    ctrl.bot.cogs.get.return_value = cog
    a = PersonalShuffleAgent(ctrl)
    bid = a.bid(_ctx("連續播我的歌單", speaker="阿明"))
    await bid.handler()
    cog.start_personal_shuffle.assert_awaited_once_with("阿明")

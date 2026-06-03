"""TDD — FindSongAgent：以「找 + 音樂錨點」識別一首歌（歌詞／主題／專輯／歌手四模式）。

與 MusicAgentV2（動詞「播/放」→ 播放）分工：本 agent 動詞「找」→ 識別歌名後交給播放路徑。
最關鍵的是誤觸防線：STT log 裡「找」367 次幾乎全是對話（找東西/找你/找工會），
複合 pattern（找 + 歌/歌詞/專輯/在講）必須一個都不誤觸。
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from intent_agents.find_song_agent import FindSongAgent, find_song_prompt
from intent_bus import IntentContext, IntentBus


def _ctx(query, *, mode="normal", speaker="Alice"):
    return IntentContext(
        speaker=speaker, raw_text=query, query=query, original_raw=query,
        wake_intent=None, stream_active=False, game_mode=(mode == "game"),
        is_owner=False, now=0.0, mode=mode,
    )


def _agent():
    ctrl = MagicMock()
    ctrl._handle_find_song = AsyncMock()
    return FindSongAgent(ctrl), ctrl


# ── mode gate ───────────────────────────────────────────────────────────────

def test_game_mode_dense_zero():
    agent, _ = _agent()
    bid = agent.bid(_ctx("找周杰倫的歌", mode="game"))
    assert bid.confidence == 0.0
    assert bid.reason == "mode_mismatch:game"


# ── 四模式 happy path（命中 + 抓對 payload）─────────────────────────────────────

def test_lyrics_mode_matches_and_captures():
    agent, _ = _agent()
    bid = agent.bid(_ctx("找歌詞有天青色等煙雨的歌"))
    assert bid.confidence == pytest.approx(0.90)
    assert bid.name == "find_song"
    assert "find_lyrics" in bid.reason


def test_theme_mode_matches_and_captures():
    agent, _ = _agent()
    bid = agent.bid(_ctx("找一首在講失戀的歌"))
    assert bid.confidence == pytest.approx(0.85)
    assert "find_theme" in bid.reason


def test_album_mode_matches():
    agent, _ = _agent()
    bid = agent.bid(_ctx("找范特西這張專輯的歌"))
    assert bid.confidence == pytest.approx(0.85)
    assert "find_album" in bid.reason


def test_artist_mode_matches():
    agent, _ = _agent()
    bid = agent.bid(_ctx("找周杰倫的歌"))
    assert bid.confidence == pytest.approx(0.80)
    assert "find_artist" in bid.reason


def test_lyrics_takes_priority_over_artist():
    """歌詞模式應比歌手模式優先（schema 順序）。"""
    agent, _ = _agent()
    bid = agent.bid(_ctx("找歌詞有海闊天空的歌"))
    assert "find_lyrics" in bid.reason


# ── slot 尾巴乾淨化：VAD 切尾常吸進語助詞/追問，要剝乾淨 ─────────────────────────
# bug: 「找歌詞天青色等煙雨啊」原本 slot 抓成「天青色等煙雨啊」→ grounded search 搜不到

@pytest.mark.asyncio
@pytest.mark.parametrize("utterance, expected_payload", [
    ("找歌詞天青色等煙雨啊", "天青色等煙雨"),
    ("找歌詞天青色等煙雨吧", "天青色等煙雨"),
    ("找歌詞天青色等煙雨嗎", "天青色等煙雨"),
    ("找歌詞天青色等煙雨喔", "天青色等煙雨"),
    ("找歌詞天青色等煙雨呢", "天青色等煙雨"),
    ("找歌詞天青色等煙雨啊啊啊", "天青色等煙雨"),    # 連續助詞
    ("找歌詞天青色等煙雨好聽嗎", "天青色等煙雨"),     # 追問尾
    ("找歌詞自由像風一樣飛翔對不對", "自由像風一樣飛翔"),
])
async def test_lyrics_slot_strips_trailing_particles(utterance, expected_payload):
    agent, ctrl = _agent()
    bid = agent.bid(_ctx(utterance))
    assert bid.confidence == pytest.approx(0.90), f"應命中 find_lyrics：{utterance!r}"
    await bid.handler()
    payload = ctrl._handle_find_song.await_args.args[1]
    assert payload == expected_payload, f"slot 沒剝乾淨：{utterance!r} → {payload!r}"


@pytest.mark.asyncio
async def test_lyrics_slot_unchanged_when_no_trailing_particle():
    """regression：歌詞本身不含末尾助詞時不該動到。"""
    agent, ctrl = _agent()
    bid = agent.bid(_ctx("找歌詞天青色等煙雨"))
    await bid.handler()
    assert ctrl._handle_find_song.await_args.args[1] == "天青色等煙雨"


@pytest.mark.asyncio
async def test_lyrics_slot_only_strip_pure_particle_tail():
    """歌詞中間有助詞字（不在尾端）不該被誤剝。"""
    agent, ctrl = _agent()
    bid = agent.bid(_ctx("找歌詞啊不要走"))  # 「啊」在中間
    await bid.handler()
    payload = ctrl._handle_find_song.await_args.args[1]
    assert payload == "啊不要走", f"中段助詞被誤剝：{payload!r}"


# ── 誤觸防線：STT log 真實對話句一個都不能中 ──────────────────────────────────────

@pytest.mark.parametrize("line", [
    "我今天大概就是館長的地圖要找他真的假的",
    "我去找你",
    "趕快去找個工會加一家",
    "隨便找活動",
    "你總能找到方法來煩我",
    "我們就要去找老師",
    "去喝酒沒找大肚",
])
def test_conversational_find_does_not_bid(line):
    agent, _ = _agent()
    bid = agent.bid(_ctx(line))
    assert bid.confidence == 0.0, f"誤觸：{line!r} 不該被當成找歌"
    assert bid.reason == "no_match"


# ── handler integration ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handler_calls_controller_with_mode_and_payload():
    agent, ctrl = _agent()
    bid = agent.bid(_ctx("找周杰倫的歌"))
    await bid.handler()
    ctrl._handle_find_song.assert_awaited_once()
    args = ctrl._handle_find_song.await_args.args
    assert args[0] == "find_artist"
    assert "周杰倫" in args[1]
    assert args[2] == "Alice"


# ── bus dispatch integration ─────────────────────────────────────────────────

# ── find_song_prompt（mode → 識別 prompt）─────────────────────────────────────

@pytest.mark.parametrize("mode", ["find_lyrics", "find_theme", "find_album", "find_artist"])
def test_find_song_prompt_includes_payload(mode):
    p = find_song_prompt(mode, "天青色等煙雨")
    assert p is not None
    assert "天青色等煙雨" in p
    assert "藝人 - 歌名" in p


def test_find_song_prompt_none_for_unknown_mode():
    assert find_song_prompt("find_bogus", "x") is None


def test_find_song_prompt_none_for_empty_payload():
    assert find_song_prompt("find_lyrics", "  ") is None


@pytest.mark.asyncio
async def test_bus_dispatches_find_song_winner():
    agent, ctrl = _agent()
    bus = IntentBus([agent])
    winner = await bus.dispatch(_ctx("找一首在講孤獨的歌"))
    assert winner is not None
    assert winner.name == "find_song"
    ctrl._handle_find_song.assert_awaited_once()

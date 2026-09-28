"""ReplayAgent — 重播當前歌曲 intent.

對應 2026-05-27 議題 E #2：L44「重播這一首」是 both-dense-zero 但實際是有效 intent。

confidence 規約：
  0.90 — 重播/再放一次/從頭/倒帶/replay

mode_compatible = {"normal", "stream"}；只在 stream_mode + 有 _current_stream_info
才 bid（radio mode 語意模糊先不做）。

Handler：
  把 _current_stream_info 插回 stream_queue 最前面
  vc.stop_playing() 觸發下一輪 picked up 同一首
  (沿用 prev_button 的 pattern，少 pop history 那段)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_bus import IntentContext


pytestmark = pytest.mark.asyncio


def _ctx(query: str, mode: str = "normal") -> IntentContext:
    return IntentContext(
        speaker="alice",
        raw_text=query,
        query=query,
        original_raw=query,
        wake_intent=0.9,
        stream_active=(mode == "stream"),
        game_mode=(mode == "game"),
        is_owner=False,
        now=0.0,
        mode=mode,
    )


def _ctrl(stream_mode=True, current_info=None):
    ctrl = MagicMock()
    ctrl.stream_mode = stream_mode
    ctrl._current_stream_info = current_info or {
        "url": "https://yt/abc",
        "title": "Test Song",
    }
    ctrl.stream_queue = []
    ctrl.bot = MagicMock()
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.stop_playing = MagicMock()
    vc.stop = MagicMock()
    ctrl.bot.voice_clients = [vc]
    ctrl.play_tts = AsyncMock()
    return ctrl, vc


# ── mode gate ─────────────────────────────────────────────────────────────


async def test_game_mode_returns_mode_mismatch():
    from intent_agents.replay_agent import ReplayAgent
    ctrl, _ = _ctrl()
    agent = ReplayAgent(ctrl)
    bid = agent.bid(_ctx("重播", mode="game"))
    assert bid.confidence == 0.0
    assert "mode_mismatch" in bid.reason


# ── playback gate ─────────────────────────────────────────────────────────


async def test_no_stream_mode_dense_zero():
    from intent_agents.replay_agent import ReplayAgent
    ctrl, _ = _ctrl(stream_mode=False)
    agent = ReplayAgent(ctrl)
    bid = agent.bid(_ctx("重播"))
    assert bid.confidence == 0.0
    assert "stream_not_active" in bid.reason


async def test_no_current_song_dense_zero():
    from intent_agents.replay_agent import ReplayAgent
    ctrl, _ = _ctrl(stream_mode=True)
    ctrl._current_stream_info = None
    agent = ReplayAgent(ctrl)
    bid = agent.bid(_ctx("重播"))
    assert bid.confidence == 0.0
    assert "no_current_song" in bid.reason


# ── replay patterns ───────────────────────────────────────────────────────


@pytest.mark.parametrize("query", [
    "重播",
    "重播這一首",
    "重播這首",
    "再放一次",
    "再播一次",
    "再聽一次",
    "倒回",
    "倒帶",
    "從頭",
    "從頭播",
    "從頭再播",
    "replay",
    "play again",
])
async def test_replay_patterns(query):
    from intent_agents.replay_agent import ReplayAgent
    ctrl, _ = _ctrl()
    agent = ReplayAgent(ctrl)
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.90, f"expected 0.90 for {query!r}, got {bid.confidence}"


# ── 確保不誤觸發既有 intents ────────────────────────────────────────────


@pytest.mark.parametrize("query", [
    "下一首",        # skip_track 的詞
    "再來一首",      # 加歌 != 重播（語意模糊，不接）
    "播放周杰倫",    # music
    "今天天氣不錯",  # 純對話
])
async def test_no_match_avoids_existing_intents(query):
    from intent_agents.replay_agent import ReplayAgent
    ctrl, _ = _ctrl()
    agent = ReplayAgent(ctrl)
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.0, f"{query!r} should not match, got {bid.confidence}"


# ── handler integration ───────────────────────────────────────────────────


async def test_handler_inserts_current_to_queue_front():
    from intent_agents.replay_agent import ReplayAgent
    ctrl, vc = _ctrl()
    ctrl.stream_queue = [{"title": "Next Song", "url": "yt/next"}]
    agent = ReplayAgent(ctrl)
    bid = agent.bid(_ctx("重播"))
    await bid.handler()
    # current 插到 queue[0]，原 Next Song 變 queue[1]
    assert ctrl.stream_queue[0]["title"] == "Test Song"
    assert ctrl.stream_queue[1]["title"] == "Next Song"


async def test_handler_stops_current_playback():
    from intent_agents.replay_agent import ReplayAgent
    ctrl, vc = _ctrl()
    ctrl._plan12 = True
    ctrl._mixer = MagicMock()
    agent = ReplayAgent(ctrl)
    bid = agent.bid(_ctx("重播"))
    await bid.handler()
    ctrl._mixer.clear_music.assert_called_once()


async def test_handler_non_plan12_falls_back_to_vc_stop():
    from intent_agents.replay_agent import ReplayAgent
    ctrl, vc = _ctrl()
    ctrl._plan12 = False
    vc.is_playing.return_value = True
    agent = ReplayAgent(ctrl)
    bid = agent.bid(_ctx("重播"))
    await bid.handler()
    assert vc.stop_playing.called or vc.stop.called


async def test_handler_plays_ack():
    from intent_agents.replay_agent import ReplayAgent
    ctrl, _ = _ctrl()
    agent = ReplayAgent(ctrl)
    bid = agent.bid(_ctx("重播"))
    await bid.handler()
    ctrl.play_tts.assert_called_once()


async def test_handler_no_voice_client_does_not_crash():
    """vc 不存在（剛斷線）→ handler 不該 raise，記 log 跳過。"""
    from intent_agents.replay_agent import ReplayAgent
    ctrl, _ = _ctrl()
    ctrl.bot.voice_clients = []  # 沒 vc
    ctrl._plan12 = False
    ctrl._mixer = None
    agent = ReplayAgent(ctrl)
    bid = agent.bid(_ctx("重播"))
    # 不該 raise
    await bid.handler()


# ── extract_title_query ─────────────────────────────────────────────────────


@pytest.mark.parametrize("query,expected", [
    ("重播一次告白氣球", "告白氣球"),
    ("再放一次告白氣球", "告白氣球"),
    ("告白氣球再放一次", "告白氣球"),
    ("從頭播告白氣球", "告白氣球"),
    ("重播", ""),
    ("重播這首", ""),
    ("再放一次", ""),
    ("replay", ""),
    ("馬文重播一次告白氣球吧", "告白氣球"),
    ("重播情歌", "情歌"),
    ("重播這首歌", ""),
])
def test_extract_title_query(query, expected):
    from intent_agents.replay_agent import extract_title_query
    assert extract_title_query(query) == expected


# ── title_matches ────────────────────────────────────────────────────────────


def test_title_matches_true_with_cruft():
    from intent_agents.replay_agent import title_matches
    assert title_matches(
        "告白氣球",
        "周杰倫 Jay Chou (特別演出: 派偉俊)【告白氣球 Love Confession】Official MV",
    ) is True


def test_title_matches_false_for_different_song():
    from intent_agents.replay_agent import title_matches
    assert title_matches(
        "告白氣球",
        "再見的時候-電影〈陽光女子合唱團〉主題曲-再見版",
    ) is False


def test_title_matches_empty_query_false():
    from intent_agents.replay_agent import title_matches
    assert title_matches("", "x") is False


# ── handler：歌名比對三分支整合測試 ──────────────────────────────────────────


def _music_cog():
    mc = MagicMock()
    mc._tail_dj_task = None
    return mc


async def test_handler_accident_reproduction_plays_named_song_via_history_url():
    """2026-09-28 事故重現：當下播《再見的時候》，使用者說「重播一次告白氣球」。
    重播X＝點歌X：用最近播過那首的 webpage_url 點歌，不切當下這首、不唸重播 ack。"""
    from intent_agents.replay_agent import ReplayAgent
    ctrl, vc = _ctrl(current_info={"url": "yt/goodbye", "title": "再見的時候"})
    baigeqiu = {"url": "googlevideo/expiring", "title": "告白氣球",
                "webpage_url": "https://www.youtube.com/watch?v=bu7nU9Mhpyo"}
    ctrl.stream_history = [baigeqiu, ctrl._current_stream_info]
    ctrl._plan12 = True
    ctrl._mixer = MagicMock()
    ctrl._safe_music_command = AsyncMock()
    mc = _music_cog()
    ctrl.bot.cogs.get.return_value = mc

    agent = ReplayAgent(ctrl)
    bid = agent.bid(_ctx("重播一次告白氣球"))
    await bid.handler()

    ctrl._safe_music_command.assert_awaited_once_with(
        "alice", "https://www.youtube.com/watch?v=bu7nU9Mhpyo", "play")
    assert ctrl.stream_queue == []
    ctrl._mixer.clear_music.assert_not_called()
    ctrl.play_tts.assert_not_called()


async def test_handler_title_matches_current_song_ignores_history():
    from intent_agents.replay_agent import ReplayAgent
    ctrl, vc = _ctrl(current_info={"url": "yt/baigeqiu", "title": "告白氣球"})
    ctrl.stream_history = [{"url": "yt/other", "title": "七里香"}, ctrl._current_stream_info]
    ctrl._plan12 = True
    ctrl._mixer = MagicMock()
    ctrl._safe_music_command = AsyncMock()
    mc = _music_cog()
    ctrl.bot.cogs.get.return_value = mc

    agent = ReplayAgent(ctrl)
    bid = agent.bid(_ctx("重播告白氣球"))
    await bid.handler()

    assert ctrl.stream_queue[0]["title"] == "告白氣球"
    ctrl._safe_music_command.assert_not_called()


async def test_handler_title_not_in_history_plays_by_title():
    from intent_agents.replay_agent import ReplayAgent
    ctrl, vc = _ctrl(current_info={"url": "yt/goodbye", "title": "再見的時候"})
    ctrl.stream_history = [ctrl._current_stream_info]
    ctrl._plan12 = True
    ctrl._mixer = MagicMock()
    ctrl._safe_music_command = AsyncMock()
    mc = _music_cog()
    ctrl.bot.cogs.get.return_value = mc

    agent = ReplayAgent(ctrl)
    bid = agent.bid(_ctx("重播一次七里香"))
    await bid.handler()

    ctrl._safe_music_command.assert_awaited_once_with("alice", "七里香", "play")
    assert ctrl.stream_queue == []
    ctrl._mixer.clear_music.assert_not_called()
    ctrl.play_tts.assert_not_called()


async def test_handler_inserted_item_is_copy_without_tail_dj_flag():
    """重播當下這首：插回佇列的是 copy 且拿掉 _dj_played_in_tail，原 dict 不動。"""
    from intent_agents.replay_agent import ReplayAgent
    ctrl, vc = _ctrl(current_info={"url": "yt/goodbye", "title": "再見的時候",
                                   "_dj_played_in_tail": True})
    ctrl.stream_history = [ctrl._current_stream_info]
    ctrl._plan12 = True
    ctrl._mixer = MagicMock()
    ctrl._safe_music_command = AsyncMock()
    mc = _music_cog()
    ctrl.bot.cogs.get.return_value = mc

    agent = ReplayAgent(ctrl)
    bid = agent.bid(_ctx("重播"))
    await bid.handler()

    assert ctrl.stream_queue[0]["title"] == "再見的時候"
    assert "_dj_played_in_tail" not in ctrl.stream_queue[0]
    assert ctrl._current_stream_info["_dj_played_in_tail"] is True
    ctrl._mixer.clear_music.assert_called_once()
    assert mc._current_song_skipped is True


async def test_handler_does_not_record_skip():
    from intent_agents.replay_agent import ReplayAgent
    ctrl, vc = _ctrl(current_info={"url": "yt/goodbye", "title": "再見的時候"})
    ctrl.stream_history = [ctrl._current_stream_info]
    ctrl._plan12 = True
    ctrl._mixer = MagicMock()
    ctrl._safe_music_command = AsyncMock()
    mc = _music_cog()
    mc._record_song_skip = MagicMock()
    ctrl.bot.cogs.get.return_value = mc

    agent = ReplayAgent(ctrl)
    bid = agent.bid(_ctx("重播"))
    await bid.handler()

    mc._record_song_skip.assert_not_called()
    for call in ctrl._safe_music_command.await_args_list:
        assert "skip" not in call.args


async def test_handler_cancels_tail_dj_task():
    from intent_agents.replay_agent import ReplayAgent
    ctrl, vc = _ctrl(current_info={"url": "yt/goodbye", "title": "再見的時候"})
    ctrl.stream_history = [ctrl._current_stream_info]
    ctrl._plan12 = True
    ctrl._mixer = MagicMock()
    ctrl._safe_music_command = AsyncMock()
    mc = _music_cog()
    task = MagicMock()
    task.done.return_value = False
    mc._tail_dj_task = task
    ctrl.bot.cogs.get.return_value = mc

    agent = ReplayAgent(ctrl)
    bid = agent.bid(_ctx("重播"))
    await bid.handler()

    task.cancel.assert_called_once()
    assert mc._tail_dj_task is None

"""PlaybackControlAgent skip negative context 修正（2026-05-27 議題 C）.

針對 5/27 分析議題 C：L19「應該下一首就是」、L32「為什麼你下一首」
被 control:skip 0.95 誤判 fast-path。

修法：post_match_filter 過濾 — modal / question word 出現在 skip 關鍵詞之前
→ 視為 chat，拒絕該 schema 命中（落到下個 schema 或 no_match）。

同樣 filter 套用 stop_playback / pause_playback（同類 FP 形狀）。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from intent_bus import IntentContext


def _ctx(query: str, stream_mode: bool = True) -> IntentContext:
    return IntentContext(
        speaker="alice",
        raw_text=query,
        query=query,
        original_raw=query,
        wake_intent=0.9,
        stream_active=stream_mode,
        game_mode=False,
        is_owner=False,
        now=0.0,
        mode="normal",
    )


def _ctrl(stream_mode: bool = True) -> MagicMock:
    ctrl = MagicMock()
    ctrl.stream_mode = stream_mode
    ctrl.play_tts = MagicMock()
    return ctrl


# ── 議題 C 的兩個原 FP case ────────────────────────────────────────────────


def test_l19_modal_prefix_does_not_match_skip():
    """L19「應該下一首就是」應該不命中 skip_track。"""
    from intent_agents.playback_control_agent import PlaybackControlAgent
    agent = PlaybackControlAgent(_ctrl())
    bid = agent.bid(_ctx("應該下一首就是"))
    assert bid.confidence == 0.0, f"expected 0.0, got {bid.confidence} ({bid.reason})"


def test_l32_question_prefix_does_not_match_skip():
    """L32「為什麼你下一首」應該不命中 skip_track。"""
    from intent_agents.playback_control_agent import PlaybackControlAgent
    agent = PlaybackControlAgent(_ctrl())
    bid = agent.bid(_ctx("為什麼你下一首"))
    assert bid.confidence == 0.0


# ── 更多 modal / question chat prefix ─────────────────────────────────────


@pytest.mark.parametrize("query", [
    "應該下一首",
    "可能下一首吧",
    "也許下一首",
    "大概下一首",
    "為什麼下一首",
    "為什麼要切歌",
    "怎麼跳過了",
    "是不是該下一首",
    "有沒有下一首啊",
    "幹嘛換歌",
])
def test_chat_prefixes_reject_skip(query):
    from intent_agents.playback_control_agent import PlaybackControlAgent
    agent = PlaybackControlAgent(_ctrl())
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.0, f"{query!r} should not match skip; got {bid.confidence}"


# ── 同 filter 套用 stop / pause ──────────────────────────────────────────


def test_modal_prefix_rejects_stop():
    from intent_agents.playback_control_agent import PlaybackControlAgent
    agent = PlaybackControlAgent(_ctrl())
    bid = agent.bid(_ctx("應該停止播放了"))
    assert bid.confidence == 0.0


def test_question_prefix_rejects_stop():
    from intent_agents.playback_control_agent import PlaybackControlAgent
    agent = PlaybackControlAgent(_ctrl())
    bid = agent.bid(_ctx("為什麼要停止播放"))
    assert bid.confidence == 0.0


def test_question_prefix_rejects_pause():
    from intent_agents.playback_control_agent import PlaybackControlAgent
    agent = PlaybackControlAgent(_ctrl())
    bid = agent.bid(_ctx("為什麼暫停"))
    assert bid.confidence == 0.0


# ── happy path（不能誤殺）────────────────────────────────────────────────


@pytest.mark.parametrize("query", ["暫停", "暫停！", "暫停。"])
def test_bare_pause_whole_utterance_matches(query):
    """8/18 bot_main.log 實測：query='暫停' 整句只有這兩字，IntentBus winner=none
    真漏接（agent 端 no_match，且 has_intent_signal 也因 <4 字被 silent，雙重看不見）。
    只做全句 anchor，不做 substring（見下方 FP 測試 why）。"""
    from intent_agents.playback_control_agent import PlaybackControlAgent
    agent = PlaybackControlAgent(_ctrl())
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.95, f"expect 0.95 for {query!r}, got {bid.confidence} ({bid.reason})"
    assert "pause" in bid.reason


def test_pause_substring_in_unrelated_sentence_does_not_match():
    """「暫停」若只是句子裡路過的詞（不是整句指令）不該誤觸發——
    真實 stt_history.log 樣本：使用者在講冷氣，不是要求暫停播放。"""
    from intent_agents.playback_control_agent import PlaybackControlAgent
    agent = PlaybackControlAgent(_ctrl())
    bid = agent.bid(_ctx("他暫暫停他的冷氣不會流出去嗎"))
    assert bid.confidence == 0.0, f"expected 0.0, got {bid.confidence} ({bid.reason})"


@pytest.mark.parametrize("query", ["暫停播放", "暫停音樂", "暫停一下", "pause"])
def test_pause_keyword_variants_match(query):
    """8/18 agent_gaps 修正：「暫停播放」原本沒在 MUSIC_PAUSE_KW 裡，
    只有 IBA-T0 無喚醒詞路徑（MUSIC_DIRECT_PAUSE_KW）接得住，wake 路徑落空
    （見 records/agent_gaps.jsonl control_pause/playback_control_pause 兩筆）。"""
    from intent_agents.playback_control_agent import PlaybackControlAgent
    agent = PlaybackControlAgent(_ctrl())
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.95, f"expect 0.95 for {query!r}, got {bid.confidence} ({bid.reason})"
    assert "pause" in bid.reason


@pytest.mark.parametrize("query", [
    "下一首",
    "切歌",
    "換歌",
    "跳過",
    "next song",
    "skip",
])
def test_keyword_only_still_matches_skip(query):
    from intent_agents.playback_control_agent import PlaybackControlAgent
    agent = PlaybackControlAgent(_ctrl())
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.95
    assert "skip" in bid.reason.lower()


def test_keyword_with_wake_prefix_still_matches():
    """「馬文下一首」這種 wake + 指令的常見形式必須命中。"""
    from intent_agents.playback_control_agent import PlaybackControlAgent
    agent = PlaybackControlAgent(_ctrl())
    bid = agent.bid(_ctx("馬文下一首"))
    assert bid.confidence == 0.95


def test_chat_marker_after_keyword_still_matches():
    """marker 出現在 keyword 之後不該觸發 filter（filter 只看 prefix）。
    e.g.「下一首為什麼那麼難聽」雖然也是 chat，但 prefix 沒 chat marker →
    本 filter 不負責此 case（後續可由 J2 chat veto 接住）。"""
    from intent_agents.playback_control_agent import PlaybackControlAgent
    agent = PlaybackControlAgent(_ctrl())
    bid = agent.bid(_ctx("下一首為什麼那麼難聽"))
    # 這個 case 暫不擋（避免複雜化 filter）
    assert bid.confidence == 0.95


# ── gate 移除後：stream_mode=False 時也應該可 match ────────────────────────


def test_stream_mode_false_still_matches_skip():
    """gate() 移除 stream_mode 限制後，stream_mode=False 仍可命中 skip_track。
    實際 skip 動作由 music_cog._handle_voice_music_command 自己判斷有沒歌在播。"""
    from intent_agents.playback_control_agent import PlaybackControlAgent
    agent = PlaybackControlAgent(_ctrl(stream_mode=False))
    bid = agent.bid(_ctx("下一首", stream_mode=False))
    assert bid.confidence == 0.95


def test_stream_mode_false_still_matches_stop():
    """stop 指令在 stream_mode=False 時也應 match。"""
    from intent_agents.playback_control_agent import PlaybackControlAgent
    agent = PlaybackControlAgent(_ctrl(stream_mode=False))
    bid = agent.bid(_ctx("停止播放", stream_mode=False))
    assert bid.confidence == 0.95


# ── Plan 12 TDD Tests ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_plan12_skip():
    """skip 指令現委派 music_cog._safe_music_command(speaker, query, 'skip')。"""
    from unittest.mock import MagicMock, AsyncMock
    from intent_agents.playback_control_agent import PlaybackControlAgent

    ctrl = _ctrl()
    mc_mock = MagicMock()
    mc_mock._safe_music_command = AsyncMock()
    ctrl.bot = MagicMock()
    ctrl.bot.cogs.get = MagicMock(return_value=mc_mock)

    agent = PlaybackControlAgent(ctrl)
    bid = agent.bid(_ctx("下一首"))
    await bid.handler()

    mc_mock._safe_music_command.assert_awaited_once()
    args = mc_mock._safe_music_command.call_args
    assert args.args[2] == "skip"


@pytest.mark.asyncio
async def test_execute_plan12_stop():
    """stop 指令委派 music_cog._safe_music_command(speaker, query, 'stop')。"""
    from unittest.mock import MagicMock, AsyncMock
    from intent_agents.playback_control_agent import PlaybackControlAgent

    ctrl = _ctrl()
    mc_mock = MagicMock()
    mc_mock._safe_music_command = AsyncMock()
    ctrl.bot = MagicMock()
    ctrl.bot.cogs.get = MagicMock(return_value=mc_mock)

    agent = PlaybackControlAgent(ctrl)
    bid = agent.bid(_ctx("停止播放"))
    await bid.handler()

    mc_mock._safe_music_command.assert_awaited_once()
    args = mc_mock._safe_music_command.call_args
    assert args.args[2] == "stop"


@pytest.mark.asyncio
async def test_execute_plan12_pause():
    """pause 指令委派 music_cog._safe_music_command(speaker, query, 'pause')。"""
    from unittest.mock import MagicMock, AsyncMock
    from intent_agents.playback_control_agent import PlaybackControlAgent

    ctrl = _ctrl()
    mc_mock = MagicMock()
    mc_mock._safe_music_command = AsyncMock()
    ctrl.bot = MagicMock()
    ctrl.bot.cogs.get = MagicMock(return_value=mc_mock)

    agent = PlaybackControlAgent(ctrl)
    bid = agent.bid(_ctx("暫停音樂"))
    await bid.handler()

    mc_mock._safe_music_command.assert_awaited_once()
    args = mc_mock._safe_music_command.call_args
    assert args.args[2] == "pause"


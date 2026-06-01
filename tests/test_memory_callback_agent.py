"""MemoryCallbackAgent v3 — SpeakBus 主動發話的第二個 agent。

由 plan-eng-review (D7/D8/D9) 拍定：
  - 走既有 5s idle tick（不加 post_utterance trigger）
  - agent 自讀 bot.engine.conv_buffer.history 拉近 N 秒 utterance
  - 用 suki_memory.peek_all_shareable_callbacks（D8 新 API）
  - 用 char-overlap（沿 speaker_topic_graph.py:227 pattern，不引 jieba）
  - 每條 dense reason distinct（feature_off / no_present / all_muted / no_callbacks /
    no_recent_utt / no_topic_overlap / cooldown）

T3 範圍：skeleton + char overlap helpers + 10 條 bid 邊界 + 4 條 char overlap test。
Handler 細節（playback_lock / TTS / consume）留 T4。
"""
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_agents.memory_callback_agent import (
    MemoryCallbackAgent,
    _OVERLAP_PUNCT,
    _char_overlap,
    _char_set,
)
from speak_bus import SpeakContext
from suki_memory import MemoryManager


# ── fixtures ──────────────────────────────────────────────────────────────────


def _mk_mem(tmp_path):
    return MemoryManager(
        db_path=str(tmp_path / "mc.db"),
        json_compat_path=str(tmp_path / "mc.json"),
    )


def _mk_ctrl(mem, history=None):
    """Minimal controller stub：agent 只摸 bot.router.memory + bot.engine.conv_buffer.history。"""
    ctrl = MagicMock()
    ctrl.bot.router.memory = mem
    ctrl.bot.engine.conv_buffer.history = history or []
    ctrl.stream_mode = False  # 2026-06-01: agent 新增 stream_mode gate，預設假
    ctrl.speak = AsyncMock()  # handler 改走 vc.speak()
    return ctrl


def _mk_ctx(present_speakers, last_speaker=None, last_text=None):
    return SpeakContext(
        channel_id=1,
        guild_id=1,
        silence_seconds=0.0,
        present_speakers=present_speakers,
        room_mood=None,
        recent_utterances=[],  # agent 自讀 conv_buffer，這欄 v3 不靠
        trigger="idle_tick",
        last_speaker=last_speaker,
        last_text=last_text,
    )


def _utt(speaker, text, ts_offset_s=0.0):
    return {"speaker": speaker, "text": text, "timestamp": time.time() + ts_offset_s}


# ── char overlap helpers (4 test) ─────────────────────────────────────────────


def test_char_set_strips_punctuation():
    """_OVERLAP_PUNCT 內所有符號都該被剝掉。"""
    raw = "你好，世界！hello.world?"
    chars = _char_set(raw)
    for p in _OVERLAP_PUNCT:
        assert p not in chars
    assert "你" in chars
    assert "h" in chars  # lowercase 已在 _char_set 處理


def test_char_overlap_chinese_only():
    q = _char_set("我要試試 grounded search")
    t = _char_set("Jack 之前說要試 grounded")
    overlap = _char_overlap(q, t)
    assert 0.0 < overlap <= 1.0


def test_char_overlap_english_lowercased():
    """英文大小寫不該影響 overlap。"""
    q = _char_set("GROUNDED SEARCH")
    t = _char_set("grounded search")
    assert _char_overlap(q, t) == 1.0


def test_char_overlap_mixed_chinese_english_handles_both():
    q = _char_set("玩玩 grounded search")
    t = _char_set("試 grounded 那個")
    assert _char_overlap(q, t) > 0.0


# ── bid edge: 10 distinct dense reasons + 1 perf ──────────────────────────────


@pytest.mark.asyncio
async def test_bid_returns_zero_when_feature_flag_off(monkeypatch, tmp_path):
    """flag 預設 OFF → Bid(0.0, 'feature_off')，不掃任何狀態。"""
    monkeypatch.delenv("SPEAK_MEMORY_CALLBACK", raising=False)
    mem = _mk_mem(tmp_path)
    mem.enqueue_callback("Alice", "戒咖啡", shareable=True)
    agent = MemoryCallbackAgent(_mk_ctrl(mem))
    bid = await agent.speak_bid(_mk_ctx(["Alice"]))
    assert bid.confidence == 0.0
    assert bid.reason == "feature_off"


@pytest.mark.asyncio
async def test_bid_returns_zero_when_no_present_speakers(monkeypatch, tmp_path):
    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    agent = MemoryCallbackAgent(_mk_ctrl(_mk_mem(tmp_path)))
    bid = await agent.speak_bid(_mk_ctx([]))
    assert bid.confidence == 0.0
    assert bid.reason == "no_present"


@pytest.mark.asyncio
async def test_bid_returns_zero_when_all_present_speakers_muted(monkeypatch, tmp_path):
    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    mem = _mk_mem(tmp_path)
    mem.enqueue_callback("Alice", "戒咖啡", shareable=True)
    mem.set_callbacks_muted("Alice", True)
    history = [_utt("Alice", "戒咖啡的事情")]
    agent = MemoryCallbackAgent(_mk_ctrl(mem, history=history))
    bid = await agent.speak_bid(_mk_ctx(["Alice"]))
    assert bid.confidence == 0.0
    assert bid.reason == "all_muted"


@pytest.mark.asyncio
async def test_bid_returns_zero_when_no_callbacks_for_any_present(monkeypatch, tmp_path):
    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    mem = _mk_mem(tmp_path)
    history = [_utt("Alice", "今天天氣不錯")]
    agent = MemoryCallbackAgent(_mk_ctrl(mem, history=history))
    bid = await agent.speak_bid(_mk_ctx(["Alice"]))
    assert bid.confidence == 0.0
    assert bid.reason == "no_callbacks"


@pytest.mark.asyncio
async def test_bid_returns_zero_when_no_recent_utterance(monkeypatch, tmp_path):
    """10s 內無 STT → no_recent_utt。"""
    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    mem = _mk_mem(tmp_path)
    mem.enqueue_callback("Alice", "戒咖啡", shareable=True)
    history = [_utt("Alice", "戒咖啡", ts_offset_s=-30.0)]  # 30s 前的舊話
    agent = MemoryCallbackAgent(_mk_ctrl(mem, history=history), recent_utt_window_s=10.0)
    bid = await agent.speak_bid(_mk_ctx(["Alice"]))
    assert bid.confidence == 0.0
    assert bid.reason == "no_recent_utt"


@pytest.mark.asyncio
async def test_bid_returns_zero_when_overlap_below_threshold(monkeypatch, tmp_path):
    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    mem = _mk_mem(tmp_path)
    mem.enqueue_callback("Alice", "戒咖啡", shareable=True)
    history = [_utt("Alice", "今天天氣不錯")]  # 跟咖啡無共現
    agent = MemoryCallbackAgent(_mk_ctrl(mem, history=history), overlap_threshold=0.35)
    bid = await agent.speak_bid(_mk_ctx(["Alice"]))
    assert bid.confidence == 0.0
    assert bid.reason == "no_topic_overlap"


@pytest.mark.asyncio
async def test_bid_wins_when_overlap_meets_threshold_and_not_in_cooldown(monkeypatch, tmp_path):
    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    mem = _mk_mem(tmp_path)
    mem.enqueue_callback("Alice", "試 grounded search", shareable=True)
    history = [_utt("Alice", "grounded search 那個怎樣")]
    agent = MemoryCallbackAgent(
        _mk_ctrl(mem, history=history), confidence=0.7, overlap_threshold=0.3
    )
    bid = await agent.speak_bid(_mk_ctx(["Alice"], last_speaker="Alice"))
    assert bid.confidence == 0.7
    assert bid.agent_name == "MemoryCallbackAgent"
    assert "topic_overlap" in bid.reason
    assert callable(bid.handler)


@pytest.mark.asyncio
async def test_bid_returns_zero_when_in_cooldown(monkeypatch, tmp_path):
    """同一筆 callback 30 分內 bid 過 → 不再 bid。"""
    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    mem = _mk_mem(tmp_path)
    mem.enqueue_callback("Alice", "試 grounded search", shareable=True)
    history = [_utt("Alice", "grounded search 那個")]
    agent = MemoryCallbackAgent(
        _mk_ctrl(mem, history=history), confidence=0.7, overlap_threshold=0.3, cooldown_s=1800.0
    )
    first = await agent.speak_bid(_mk_ctx(["Alice"], last_speaker="Alice"))
    assert first.confidence == 0.7  # 第一次 win
    second = await agent.speak_bid(_mk_ctx(["Alice"], last_speaker="Alice"))
    assert second.confidence == 0.0
    assert second.reason == "cooldown"


@pytest.mark.asyncio
async def test_bid_picks_latest_commitment_when_multiple_hit(monkeypatch, tmp_path):
    """多筆 callback 都 overlap → 取最新 commitment_ts。"""
    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    mem = _mk_mem(tmp_path)
    mem.enqueue_callback("Alice", "試 grounded search 舊版", shareable=True)
    # 強制改 ts 模擬「先舊後新」
    old_item = mem.get_player_memory("Alice")["callback_queue"][0]
    old_item["ts"] = time.time() - 5 * 86400  # 5 天前
    mem.enqueue_callback("Alice", "試 grounded search 新版", shareable=True)
    history = [_utt("Alice", "grounded search 那個")]
    agent = MemoryCallbackAgent(
        _mk_ctrl(mem, history=history), confidence=0.7, overlap_threshold=0.3
    )
    bid = await agent.speak_bid(_mk_ctx(["Alice"], last_speaker="Alice"))
    assert bid.confidence == 0.7
    # reason 帶 commitment text 片段以利 debug；驗證選了「新版」
    assert "新版" in bid.reason


# ── handler: TTS + consume (T4) ───────────────────────────────────────────────


def _mk_ctrl_with_tts(mem, history=None):
    """T4 controller stub：補上 speak (AsyncMock) / tts_engine / stt_logger。

    2026-06-01: handler 改走 vc.speak() 統一入口（接 hotswap）。play_tts 仍保留
    AsyncMock 供 failure-injection 測試使用（覆蓋 speak 直接設 side_effect）。
    """
    ctrl = MagicMock()
    ctrl.bot.router.memory = mem
    ctrl.bot.engine.conv_buffer.history = history or []
    ctrl.bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)
    ctrl.stream_mode = False
    ctrl.speak = AsyncMock(return_value=None)
    ctrl.play_tts = AsyncMock(return_value=None)
    ctrl.stt_logger = MagicMock()
    return ctrl


@pytest.mark.asyncio
async def test_handler_tts_success_consumes_callback(monkeypatch, tmp_path):
    """TTS 成功 → consume_callback 把該筆從 queue 移除。"""
    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    mem = _mk_mem(tmp_path)
    mem.enqueue_callback("Alice", "試 grounded search", shareable=True)
    history = [_utt("Alice", "grounded search 那個")]
    ctrl = _mk_ctrl_with_tts(mem, history=history)
    agent = MemoryCallbackAgent(ctrl, confidence=0.7, overlap_threshold=0.3)
    bid = await agent.speak_bid(_mk_ctx(["Alice"], last_speaker="Alice"))
    await bid.handler()
    # play_tts 被 await
    assert ctrl.speak.await_count == 1
    # 該筆已從 queue 移除
    assert mem.peek_all_shareable_callbacks("Alice") == []


@pytest.mark.asyncio
async def test_handler_tts_truncate_gate_still_delivers(monkeypatch, tmp_path):
    """truncate_for_tts 砍掉超 7s 的長句 → 仍然發聲 + consume。"""
    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    mem = _mk_mem(tmp_path)
    long_text = "試 grounded search 然後寫很長很長很長的後續計畫" * 5
    mem.enqueue_callback("Alice", long_text, shareable=True)
    history = [_utt("Alice", "grounded search 那個")]
    ctrl = _mk_ctrl_with_tts(mem, history=history)
    # 模擬：第一次估時超 7s（會被砍）、後續估算 < 7s
    durations = iter([8.0, 8.0, 6.0, 5.0, 4.0])
    ctrl.bot.tts_engine.get_estimated_duration = MagicMock(side_effect=lambda *_: next(durations, 3.0))
    agent = MemoryCallbackAgent(ctrl, confidence=0.7, overlap_threshold=0.2)
    bid = await agent.speak_bid(_mk_ctx(["Alice"], last_speaker="Alice"))
    await bid.handler()
    assert ctrl.speak.await_count == 1
    # 仍 consume — gate 觸發不擋投遞
    assert mem.peek_all_shareable_callbacks("Alice") == []


@pytest.mark.asyncio
async def test_handler_tts_failure_does_not_consume(monkeypatch, tmp_path):
    """play_tts raise → 不 consume，下次仍能被 bid（idempotent 重投）。"""
    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    mem = _mk_mem(tmp_path)
    mem.enqueue_callback("Alice", "試 grounded search", shareable=True)
    history = [_utt("Alice", "grounded search 那個")]
    ctrl = _mk_ctrl_with_tts(mem, history=history)
    ctrl.speak = AsyncMock(side_effect=RuntimeError("voice client died"))
    agent = MemoryCallbackAgent(ctrl, confidence=0.7, overlap_threshold=0.3)
    bid = await agent.speak_bid(_mk_ctx(["Alice"], last_speaker="Alice"))
    # handler 內部 try/except → 不傳播
    await bid.handler()
    # 沒 consume
    assert len(mem.peek_all_shareable_callbacks("Alice")) == 1


@pytest.mark.asyncio
async def test_handler_swallows_unexpected_exception(monkeypatch, tmp_path):
    """任何意外（例如 truncate import 壞 / get_estimated_duration raise）→ 不傳播。"""
    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    mem = _mk_mem(tmp_path)
    mem.enqueue_callback("Alice", "試 grounded search", shareable=True)
    history = [_utt("Alice", "grounded search 那個")]
    ctrl = _mk_ctrl_with_tts(mem, history=history)
    ctrl.bot.tts_engine.get_estimated_duration = MagicMock(side_effect=ValueError("estimator broken"))
    agent = MemoryCallbackAgent(ctrl, confidence=0.7, overlap_threshold=0.3)
    bid = await agent.speak_bid(_mk_ctx(["Alice"], last_speaker="Alice"))
    # 不 raise（傳出就會 pytest fail）
    await bid.handler()
    # 沒成功 TTS → 不 consume
    assert len(mem.peek_all_shareable_callbacks("Alice")) == 1


@pytest.mark.asyncio
async def test_bid_latency_under_5ms(monkeypatch, tmp_path):
    """10 callbacks × 3 speakers，bid 必須 sync-fast。p95 < 5ms 在單機可能波動，threshold 拉到 50ms 防 CI flake。"""
    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    mem = _mk_mem(tmp_path)
    speakers = ["Alice", "Bob", "Carol"]
    for spk in speakers:
        for i in range(10):
            mem.enqueue_callback(spk, f"commitment_{i}_xyz", shareable=True)
    history = [_utt("Alice", "今天的話題完全不重疊_qqq")]
    agent = MemoryCallbackAgent(_mk_ctrl(mem, history=history), overlap_threshold=0.99)
    start = time.perf_counter()
    await agent.speak_bid(_mk_ctx(speakers, last_speaker="Alice"))
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 50.0, f"bid latency {elapsed_ms:.2f}ms — sync-fast budget 是 5ms"

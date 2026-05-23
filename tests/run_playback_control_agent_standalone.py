"""tests/run_playback_control_agent_standalone.py — M5 PlaybackControlAgent

Run: venv_simon/bin/python tests/run_playback_control_agent_standalone.py
"""
from __future__ import annotations

import asyncio
import sys
import time
import traceback
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from intent_agents.playback_control_agent import PlaybackControlAgent, SKIP_BLACKLIST_THRESHOLD
from intent_bus import IntentContext

PASSED = 0
FAILED = 0
FAILURES = []


def run(name, fn):
    global PASSED, FAILED
    try:
        if asyncio.iscoroutinefunction(fn):
            asyncio.run(fn())
        else:
            fn()
        print(f"  ✓ {name}")
        PASSED += 1
    except AssertionError as e:
        print(f"  ✗ {name}: {e}")
        FAILURES.append((name, traceback.format_exc()))
        FAILED += 1
    except Exception as e:
        print(f"  ✗ {name} ERROR: {type(e).__name__}: {e}")
        FAILURES.append((name, traceback.format_exc()))
        FAILED += 1


# ── Helpers ──────────────────────────────────────────────────────────────────

def _mk_ctx(query="下一首", speaker="alice", mode="stream"):
    return IntentContext(
        speaker=speaker, raw_text=query, query=query,
        original_raw=query, wake_intent=0.95,
        stream_active=(mode == "stream"), game_mode=(mode == "game"),
        is_owner=False, now=time.time(), mode=mode,
    )


def _mk_ctrl(stream_mode=True, voice_client_methods=("stop_playing", "stop", "pause")):
    """假 controller with voice_client + play_tts mock."""
    ctrl = MagicMock()
    ctrl.stream_mode = stream_mode
    ctrl.stream_queue = []
    ctrl.stream_paused = False
    ctrl._current_stream_info = {"url": "https://youtu.be/abc", "title": "Test Song"}
    ctrl._consecutive_skips_by_url = {}
    ctrl._cover_blacklist = None
    ctrl.play_tts = AsyncMock()

    vc = MagicMock()
    for method in voice_client_methods:
        setattr(vc, method, MagicMock())
    vc.is_connected = MagicMock(return_value=True)
    ctrl.bot = MagicMock()
    ctrl.bot.voice_clients = [vc]
    ctrl._test_vc = vc  # for assertions
    return ctrl


# ── mode gate ────────────────────────────────────────────────────────────────

def t_game_mode_dense_zero():
    """game mode → mode_mismatch dense 0.0 bid."""
    agent = PlaybackControlAgent(_mk_ctrl())
    bid = agent.bid(_mk_ctx(mode="game"))
    assert bid.confidence == 0.0
    assert "mode_mismatch" in bid.reason


def t_stream_not_active_gate():
    """stream_mode=False → stream_not_active gate."""
    ctrl = _mk_ctrl(stream_mode=False)
    agent = PlaybackControlAgent(ctrl)
    bid = agent.bid(_mk_ctx())
    assert bid.confidence == 0.0
    assert bid.reason == "stream_not_active"


# ── intent match ─────────────────────────────────────────────────────────────

def t_skip_track_match():
    agent = PlaybackControlAgent(_mk_ctrl())
    for query in ("下一首", "切歌", "換歌", "skip", "next song", "跳過"):
        bid = agent.bid(_mk_ctx(query=query))
        assert bid.confidence == 0.85, f"expect 0.85 for '{query}' got {bid.confidence} ({bid.reason})"
        assert "skip" in bid.reason


def t_stop_playback_match():
    agent = PlaybackControlAgent(_mk_ctrl())
    for query in ("停止播放", "別播了", "stop play"):
        bid = agent.bid(_mk_ctx(query=query))
        assert bid.confidence == 0.85, f"expect 0.85 for '{query}' got {bid.confidence} ({bid.reason})"
        assert "stop" in bid.reason


def t_pause_playback_match():
    agent = PlaybackControlAgent(_mk_ctrl())
    for query in ("暫停", "pause"):
        bid = agent.bid(_mk_ctx(query=query))
        assert bid.confidence == 0.80, f"expect 0.80 for '{query}' got {bid.confidence}"


def t_unrelated_no_match():
    agent = PlaybackControlAgent(_mk_ctrl())
    for query in ("天氣怎樣", "聊天", "點歌"):
        bid = agent.bid(_mk_ctx(query=query))
        assert bid.confidence == 0.0, f"expect 0 for unrelated '{query}' got {bid.confidence}"


# ── handler — skip ───────────────────────────────────────────────────────────

async def t_skip_handler_calls_vc_stop():
    ctrl = _mk_ctrl()
    agent = PlaybackControlAgent(ctrl)
    bid = agent.bid(_mk_ctx(query="下一首", speaker="alice"))
    await bid.handler()
    # stop_playing 應被呼叫
    assert ctrl._test_vc.stop_playing.called, "vc.stop_playing 應該被呼叫"
    # quick ack 應該觸發
    assert ctrl.play_tts.await_count >= 1, "play_tts 應該被呼叫"
    call_args = ctrl.play_tts.await_args
    assert "好" in call_args.args[0] or "換" in call_args.args[0], f"ack 內容應含'好/換': {call_args}"


async def t_skip_parallel_ack_and_action():
    """ack + action 並行（兩個 task 都被 await）。"""
    ctrl = _mk_ctrl()
    # 讓 play_tts 慢一點，確保並行
    async def _slow_ack(*args, **kwargs):
        await asyncio.sleep(0.05)
    ctrl.play_tts = AsyncMock(side_effect=_slow_ack)
    agent = PlaybackControlAgent(ctrl)
    bid = agent.bid(_mk_ctx(query="下一首", speaker="alice"))
    t0 = time.time()
    await bid.handler()
    elapsed = time.time() - t0
    # 並行 → 應該 ~50ms 而不是 stop+ack 串行
    assert elapsed < 0.5, f"並行應快，elapsed={elapsed:.2f}s"
    assert ctrl._test_vc.stop_playing.called


# ── handler — skip auto-blacklist ────────────────────────────────────────────

async def t_single_skip_not_blacklisted():
    """單一 speaker skip 一次 → 不該加黑名單。"""
    ctrl = _mk_ctrl()
    agent = PlaybackControlAgent(ctrl)
    bid = agent.bid(_mk_ctx(query="下一首", speaker="alice"))
    await bid.handler()
    # tracker 應該記錄 alice
    assert "https://youtu.be/abc" in ctrl._consecutive_skips_by_url
    assert ctrl._consecutive_skips_by_url["https://youtu.be/abc"] == {"alice"}
    # blacklist 未加（因為只 1 個 speaker）
    bl = getattr(ctrl, "_cover_blacklist", None)
    assert bl is None or not bl.is_blacklisted("https://youtu.be/abc"), "1 個 speaker 不該觸發 blacklist"


async def t_two_speakers_skip_blacklist_added():
    """同一 url 被 2 不同 speaker skip → 自動加黑名單。"""
    ctrl = _mk_ctrl()
    agent = PlaybackControlAgent(ctrl)
    # alice skip
    bid1 = agent.bid(_mk_ctx(query="下一首", speaker="alice"))
    await bid1.handler()
    # bob skip 同一首
    bid2 = agent.bid(_mk_ctx(query="下一首", speaker="bob"))
    await bid2.handler()
    # 黑名單應該已加
    bl = ctrl._cover_blacklist
    assert bl is not None, "blacklist 應該已被 lazy init"
    assert bl.is_blacklisted("https://youtu.be/abc"), "2 speaker skip 應該觸發 blacklist"
    # tracker entry 應該被清掉（避免重覆加）
    assert "https://youtu.be/abc" not in ctrl._consecutive_skips_by_url


async def t_same_speaker_twice_no_blacklist():
    """同一 speaker skip 同一 url 兩次 → set 仍只 1 個 speaker → 不加。"""
    ctrl = _mk_ctrl()
    agent = PlaybackControlAgent(ctrl)
    bid1 = agent.bid(_mk_ctx(query="下一首", speaker="alice"))
    await bid1.handler()
    bid2 = agent.bid(_mk_ctx(query="下一首", speaker="alice"))
    await bid2.handler()
    bl = getattr(ctrl, "_cover_blacklist", None)
    assert bl is None or not bl.is_blacklisted("https://youtu.be/abc"), "同人 skip 2 次不該觸發"


# ── handler — stop ───────────────────────────────────────────────────────────

async def t_stop_handler_clears_queue():
    ctrl = _mk_ctrl()
    ctrl.stream_queue = [{"title": "A"}, {"title": "B"}]
    agent = PlaybackControlAgent(ctrl)
    bid = agent.bid(_mk_ctx(query="停止播放", speaker="alice"))
    await bid.handler()
    assert ctrl._test_vc.stop.called
    assert ctrl.stream_queue == [], "stop 應該清空 queue"
    # 不影響 stream_paused
    assert ctrl.stream_paused is False


# ── handler — pause ──────────────────────────────────────────────────────────

async def t_pause_handler_sets_paused():
    ctrl = _mk_ctrl()
    agent = PlaybackControlAgent(ctrl)
    bid = agent.bid(_mk_ctx(query="暫停", speaker="alice"))
    await bid.handler()
    assert ctrl._test_vc.pause.called
    assert ctrl.stream_paused is True


# ── handler — voice_client missing ───────────────────────────────────────────

async def t_no_voice_client_no_crash():
    ctrl = _mk_ctrl()
    ctrl.bot.voice_clients = []  # 無連線
    agent = PlaybackControlAgent(ctrl)
    bid = agent.bid(_mk_ctx(query="下一首", speaker="alice"))
    # 不該 raise
    await bid.handler()
    # play_tts 還是會被呼叫（ack 仍跑）
    assert ctrl.play_tts.await_count >= 1


# ── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== M5 PlaybackControlAgent standalone tests ===\n")

    print("Mode gate:")
    run("game mode → mode_mismatch", t_game_mode_dense_zero)
    run("stream_not_active gate", t_stream_not_active_gate)
    print()

    print("Intent match:")
    run("skip_track patterns", t_skip_track_match)
    run("stop_playback patterns", t_stop_playback_match)
    run("pause_playback patterns", t_pause_playback_match)
    run("unrelated → no_match", t_unrelated_no_match)
    print()

    print("Skip handler:")
    run("vc.stop_playing called", t_skip_handler_calls_vc_stop)
    run("parallel ack + action", t_skip_parallel_ack_and_action)
    print()

    print("Auto-blacklist (D3 A 方案):")
    run("1 speaker skip → no blacklist", t_single_skip_not_blacklisted)
    run("2 different speakers → blacklist", t_two_speakers_skip_blacklist_added)
    run("same speaker 2x → no blacklist", t_same_speaker_twice_no_blacklist)
    print()

    print("Stop handler:")
    run("stop clears queue", t_stop_handler_clears_queue)
    print()

    print("Pause handler:")
    run("pause sets stream_paused", t_pause_handler_sets_paused)
    print()

    print("No voice_client:")
    run("no vc no crash", t_no_voice_client_no_crash)

    print()
    print(f"=== Results: {PASSED} passed, {FAILED} failed ===")
    if FAILED:
        print("\n--- Failures ---")
        for name, tb in FAILURES:
            print(f"\n{name}:")
            print(tb)
        sys.exit(1)

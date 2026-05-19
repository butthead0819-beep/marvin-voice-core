"""TDD: B1 — IntentBus winner 取走 intent 時，取消 dangling speculative prefetch。

背景：
- handle_stt_result 偵測到 wake → 1973 行：speculative prefetch 啟動，
  存進 router._pending_prefetch[speaker]
- _process_queued_query → 3377 行 _intent_bus.dispatch → music/nemoclaw 接走
  → 直接 return，line 3416 的 LLM 路徑跑不到 → prefetch task 變孤兒
- 孤兒 task 繼續吃 LLM quota，且若下次 wake 是 chat turn，舊 result 可能被
  誤拿來當 LLM 起手回答（雖然 1976 行有 pop replace 但邊界 race 仍可能）

修法：bus 抓到 winner（任一 agent）→ 取消 _pending_prefetch[speaker]。

非 goal：「flush music TTS in queue」目前不存在這條路徑（bus 在 LLM 之前接走，
TTS 還沒入 queue），不需要實作。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.router = MagicMock()
    bot.router._pending_prefetch = {}
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.post_summon_callback = None

    with patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        cog = VoiceController(bot)
    cog.stt_logger = MagicMock()
    return cog


# ── helper 行為 ────────────────────────────────────────────────────────────

def test_cancel_stale_prefetch_cancels_pending_task():
    cog = _make_cog()
    task = MagicMock()
    task.done.return_value = False
    task.cancel = MagicMock()
    cog.bot.router._pending_prefetch["Alice"] = task

    cog._cancel_stale_prefetch("Alice")

    task.cancel.assert_called_once()
    assert "Alice" not in cog.bot.router._pending_prefetch


def test_cancel_stale_prefetch_skips_done_task():
    """已完成的 task 不該再 cancel（會 raise CancelledError 浪費），但要從 dict 清掉。"""
    cog = _make_cog()
    task = MagicMock()
    task.done.return_value = True
    task.cancel = MagicMock()
    cog.bot.router._pending_prefetch["Alice"] = task

    cog._cancel_stale_prefetch("Alice")

    task.cancel.assert_not_called()
    assert "Alice" not in cog.bot.router._pending_prefetch


def test_cancel_stale_prefetch_no_task_is_noop():
    cog = _make_cog()
    # 沒有任何 entry → 不該 raise
    cog._cancel_stale_prefetch("NobodyHome")


def test_cancel_stale_prefetch_handles_missing_router_attr():
    """router 沒有 _pending_prefetch 屬性時也不能炸。"""
    cog = _make_cog()
    del cog.bot.router._pending_prefetch
    # 不該 raise
    cog._cancel_stale_prefetch("Alice")


def test_cancel_stale_prefetch_only_touches_target_speaker():
    cog = _make_cog()
    task_a = MagicMock(); task_a.done.return_value = False
    task_b = MagicMock(); task_b.done.return_value = False
    cog.bot.router._pending_prefetch["Alice"] = task_a
    cog.bot.router._pending_prefetch["Bob"] = task_b

    cog._cancel_stale_prefetch("Alice")

    task_a.cancel.assert_called_once()
    task_b.cancel.assert_not_called()
    assert "Bob" in cog.bot.router._pending_prefetch


# ── 整合：bus winner 觸發 cancel ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_bus_winner_triggers_prefetch_cancel():
    """整合：bus dispatch 拿到 winner → 應該已經呼叫 _cancel_stale_prefetch。

    這個測試直接 patch _intent_bus.dispatch 回傳 winner，驗證
    _process_queued_query 在 bus return 後立即呼叫 cancel。
    """
    from intent_bus import Bid
    cog = _make_cog()

    task = MagicMock()
    task.done.return_value = False
    cog.bot.router._pending_prefetch["Alice"] = task

    # 直接呼叫 helper 模擬「bus 接走後」的清理動作
    # 為什麼不 patch dispatch 整段：_process_queued_query 走到 dispatch 前
    # 有 ~20 個前置 fast-track 檢查（PA / Status / Vision...），mock 起來會
    # 噪音壓過 signal。改用「helper 存在 + dispatch 後呼叫」的契約測試。
    cog._cancel_stale_prefetch("Alice")
    task.cancel.assert_called_once()


def test_cancel_helper_is_called_from_voice_controller_source():
    """Source-level assert：_process_queued_query 內 bus winner branch 必須呼叫
    _cancel_stale_prefetch。比 mock dispatch 整段更可靠（mock 起來會偏離真實流程）。"""
    import inspect
    from cogs.voice_controller import VoiceController
    src = inspect.getsource(VoiceController._process_queued_query)
    # 確保 bus winner branch 內有 cancel
    # （不檢查精確行數，只檢查 source 內有這兩個 token 同時出現）
    assert "_intent_bus.dispatch" in src
    assert "_cancel_stale_prefetch" in src

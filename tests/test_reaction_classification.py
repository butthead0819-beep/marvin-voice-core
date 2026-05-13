"""
_classify_and_log_reaction — 嚴重分類修正 + STT 雜訊過濾測試。

Rules:
  1. wake_latency > 20s + 無有效反應 → reaction_type="延遲"，不算嚴重
  2. wake_latency <= 20s + 無有效反應 → reaction_type="嚴重"
  3. wake_latency=None + 無有效反應 → reaction_type="嚴重"（保守處理）
  4. 含 <Background>/<Target> XML 標籤的 reaction_entry → 過濾掉（STT context 雜訊）
  5. 過濾後有效反應存在 → 走 LLM 分類（不受 latency 影響）
  6. LLM 分類結果寫入 record 的 reaction_type
  7. 過濾後所有 reaction_entries 都是雜訊 + latency > 20s → 延遲
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.get_estimated_duration.return_value = 2.0
    bot.router = MagicMock()
    bot.router._call_llm = AsyncMock(return_value=json.dumps({"type": "錯誤", "reason": "default"}))

    with patch("discord_voice_engine.faster_whisper", None, create=True):
        from discord_voice_engine import DiscordVoiceEngine
        engine = DiscordVoiceEngine(bot)
    bot.engine = engine

    with patch("discord.ext.tasks.loop", lambda *a, **kw: lambda f: f), \
         patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        cog = VoiceController(bot)

    cog.log_buffer = []
    cog.stt_logger = MagicMock()
    cog.stt_logger.info = MagicMock()
    return cog


# ── 1. latency guard ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_high_latency_no_reaction_yields_latency_type():
    """wake_latency=40s + 無反應 → reaction_type='延遲'，不算嚴重。"""
    cog = _make_cog()
    written_record = {}

    with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread, \
         patch("os.makedirs"):
        async def _run_fn(fn):
            fn()
        mock_thread.side_effect = _run_fn

        with patch("builtins.open", unittest_mock_open(written_record)):
            await cog._classify_and_log_reaction(
                speaker="showay",
                bot_response="我在這裡",
                reaction_entries=[],
                respond_time=1000.0,
                wake_latency=40.0,
            )

    assert written_record.get("reaction_type") == "延遲"


@pytest.mark.asyncio
async def test_low_latency_no_reaction_yields_severe():
    """wake_latency=5s + 無反應 → reaction_type='嚴重'。"""
    cog = _make_cog()

    written_record = {}

    def _capture_write(content):
        written_record.update(json.loads(content.strip()))

    with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread, \
         patch("os.makedirs"):
        # Simulate asyncio.to_thread calling the function
        async def _run_fn(fn):
            fn()
        mock_thread.side_effect = _run_fn

        with patch("builtins.open", unittest_mock_open(written_record)):
            await cog._classify_and_log_reaction(
                speaker="showay",
                bot_response="我在這裡",
                reaction_entries=[],
                respond_time=1000.0,
                wake_latency=5.0,
            )

    assert written_record.get("reaction_type") == "嚴重"


@pytest.mark.asyncio
async def test_none_latency_no_reaction_yields_severe():
    """wake_latency=None + 無反應 → 保守地分類為 '嚴重'。"""
    cog = _make_cog()
    written_record = {}

    with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread, \
         patch("os.makedirs"):
        async def _run_fn(fn):
            fn()
        mock_thread.side_effect = _run_fn

        with patch("builtins.open", unittest_mock_open(written_record)):
            await cog._classify_and_log_reaction(
                speaker="showay",
                bot_response="我在這裡",
                reaction_entries=[],
                respond_time=1000.0,
                wake_latency=None,
            )

    assert written_record.get("reaction_type") == "嚴重"


# ── 2. STT noise filter ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_background_tag_entries_filtered_as_noise():
    """含 <Background> XML 的 entry 過濾掉後若無有效反應 + latency=40s → 延遲。"""
    cog = _make_cog()
    written_record = {}

    noise_entry = "<Background>\nshoway：<Background>\n狗與鹿：<Target>起床了嗎</Target>\n</Background>"

    with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread, \
         patch("os.makedirs"):
        async def _run_fn(fn):
            fn()
        mock_thread.side_effect = _run_fn

        with patch("builtins.open", unittest_mock_open(written_record)):
            await cog._classify_and_log_reaction(
                speaker="showay",
                bot_response="我在這裡",
                reaction_entries=[noise_entry],
                respond_time=1000.0,
                wake_latency=40.0,
            )

    assert written_record.get("reaction_type") == "延遲"


@pytest.mark.asyncio
async def test_very_short_entries_filtered_as_noise():
    """< 5 字的 entry 過濾掉後若無有效反應 + latency=40s → 延遲。"""
    cog = _make_cog()
    written_record = {}

    with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread, \
         patch("os.makedirs"):
        async def _run_fn(fn):
            fn()
        mock_thread.side_effect = _run_fn

        with patch("builtins.open", unittest_mock_open(written_record)):
            await cog._classify_and_log_reaction(
                speaker="showay",
                bot_response="我在這裡",
                reaction_entries=["嗯"],        # 1 字，雜訊
                respond_time=1000.0,
                wake_latency=40.0,
            )

    assert written_record.get("reaction_type") == "延遲"


@pytest.mark.asyncio
async def test_valid_entry_bypasses_latency_guard():
    """有效 entry（非雜訊）→ 走 LLM 分類，不受 latency 影響。"""
    cog = _make_cog()
    cog.bot.router._call_llm = AsyncMock(
        return_value=json.dumps({"type": "喜歡", "reason": "正面回應"})
    )
    written_record = {}

    with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread, \
         patch("os.makedirs"):
        async def _run_fn(fn):
            fn()
        mock_thread.side_effect = _run_fn

        with patch("builtins.open", unittest_mock_open(written_record)):
            await cog._classify_and_log_reaction(
                speaker="showay",
                bot_response="我在這裡",
                reaction_entries=["哈哈哈哈超有趣"],  # 有效（> 4 字，無 XML）
                respond_time=1000.0,
                wake_latency=40.0,  # 高延遲，但有有效反應 → 走 LLM
            )

    assert written_record.get("reaction_type") == "喜歡"


# ── helpers ──────────────────────────────────────────────────────────────────

def unittest_mock_open(capture_dict: dict):
    """回傳一個 open() mock，write() 時把 JSON 解析到 capture_dict。"""
    from unittest.mock import mock_open
    mo = mock_open()
    def _write(content):
        line = content.strip()
        if line:
            try:
                capture_dict.update(json.loads(line))
            except json.JSONDecodeError:
                pass
    mo.return_value.__enter__.return_value.write.side_effect = _write
    return mo

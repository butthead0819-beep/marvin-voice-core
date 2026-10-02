"""TDD — 日記停止貼到 Discord（2026-10-02 Jack 拍板）。

慢速迴圈產生的日記摘要照常寫入 records/chat_summary_log.txt（供 DJ 串場取用），
但不再發送給 Discord 頻道，也不會建立專屬日記頻道。
pending_intervention 檔案同樣需被清理並重置為 None。
"""
from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_fake_with_guild(*, summary_return="這是一段測試摘要", pending_file=None):
    fake = MagicMock()
    fake.bot.engine.conv_buffer = MagicMock()
    fake.last_player_speech_time = time.time()
    now = time.time()
    entries = [
        {"speaker": "大肚", "text": "今天公司在吵要不要導入新的排班系統，吵了快一小時，大家意見都不一樣，主管也拿不定主意，會議一直拖延，最後決定下週再開一次會討論", "timestamp": now},
        {"speaker": "狗與露", "text": "對啊而且那個系統介面超難用，大家都在抱怨，說根本沒人會用，教學文件也寫得亂七八糟看不懂，客服也不太理人", "timestamp": now},
        {"speaker": "Alice", "text": "我們部門已經在用類似的東西了，其實還好，習慣就好用了，一開始也是覺得卡卡的後來就順了，可能是每個系統都要適應期", "timestamp": now},
        {"speaker": "大肚", "text": "希望下週開會能有個結論，不然這樣拖下去大家都很煩躁，工作效率也受影響", "timestamp": now},
    ]
    fake.bot.engine.conv_buffer.pop_new_entries.return_value = entries
    fake.slow_loop_accumulator = []
    fake.bot.router.current_game = None
    fake._stt_call_counter = 0
    fake.get_online_members.return_value = ["大肚", "狗與露", "Alice"]
    fake.bot.router.generate_slow_summary = AsyncMock(return_value=summary_return)
    fake.pending_intervention = {"file_path": pending_file, "text": "待播獨白"} if pending_file else None

    # 模擬 Discord guild 與 text_channel
    channel = MagicMock()
    channel.send = AsyncMock()
    guild = MagicMock()
    guild.text_channels = []
    guild.create_text_channel = AsyncMock(return_value=channel)
    channel.guild = guild
    fake.active_text_channel = channel

    return fake, guild, channel


@pytest.mark.asyncio
async def test_slow_loop_writes_rag_log_but_never_posts_diary_or_creates_channel(tmp_path, monkeypatch):
    """slow loop 產生 summary 時：寫入 chat_summary_log.txt，但不建立頻道、不貼到 Discord。"""
    from cogs.voice_controller import VoiceController

    monkeypatch.chdir(tmp_path)
    summary_text = "核心：大家今天在討論新系統的排班爭議。"
    fake, guild, channel = _make_fake_with_guild(summary_return=summary_text)

    await VoiceController.slow_system_loop.coro(fake)

    # 1. records/chat_summary_log.txt 照常寫入
    log_path = tmp_path / "records" / "chat_summary_log.txt"
    assert log_path.exists(), "summary 必須照常寫入 chat_summary_log.txt 供 DJ 串場使用"
    assert summary_text in log_path.read_text(encoding="utf-8")

    # 2. 頻道建立與訊息發送均不得觸發
    guild.create_text_channel.assert_not_called()
    channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_slow_loop_cleans_pending_intervention_without_posting(tmp_path, monkeypatch):
    """pending_intervention 檔案應被清理且屬性重設為 None，但同樣不貼文。"""
    from cogs.voice_controller import VoiceController

    monkeypatch.chdir(tmp_path)
    pending_file = tmp_path / "scratch_intervention.wav"
    pending_file.write_bytes(b"RIFF dummy audio")
    assert pending_file.exists()

    summary_text = "核心：測試中清理暫存音檔。"
    fake, guild, channel = _make_fake_with_guild(summary_return=summary_text, pending_file=str(pending_file))

    await VoiceController.slow_system_loop.coro(fake)

    # 暫存音檔已被刪除、pending_intervention 被置空
    assert not pending_file.exists(), "暫存的 pending_intervention 音檔必須被清理"
    assert fake.pending_intervention is None

    # 不貼文
    guild.create_text_channel.assert_not_called()
    channel.send.assert_not_called()

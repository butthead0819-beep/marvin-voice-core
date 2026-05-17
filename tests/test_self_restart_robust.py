"""TDD — self_restart 必須在所有 pre-execv 失敗下仍走到 os.execv。

歷史 bug：MemoryManager 從 JSON 重構成 SQLite 時，舊版 _save_data() 被刪除
但呼叫點沒清乾淨。導致 /marvin_reboot 噴 AttributeError 卡死沒重啟，
使用者卻以為 bot 已重啟 → 跑舊 code 卻不知道。

這組測試保證未來任何 pre-execv 步驟失敗都不會阻斷 os.execv 執行。
"""
from __future__ import annotations
import pytest
import sys
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def cog_with_mocks():
    """建一個 mock 的 cog instance，attribute 都注入 mock。"""
    from cogs.voice_controller import VoiceController

    bot = MagicMock()
    bot.router.memory.flush = MagicMock()
    bot.close = AsyncMock()
    bot.last_restart_time = 0
    bot.cogs = {}

    # 不走 __init__（會啟太多 task）。直接建空殼塞 attribute。
    cog = VoiceController.__new__(VoiceController)
    cog.bot = bot
    cog.active_text_channel = None
    return cog


@pytest.mark.asyncio
async def test_self_restart_calls_execv_on_normal_path(cog_with_mocks):
    cog = cog_with_mocks

    with patch("os.execv") as execv_mock, \
         patch("asyncio.create_subprocess_exec") as subp_mock:
        # mock subprocess 回傳 rc=0
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"Already up to date.", b""))
        proc.returncode = 0
        subp_mock.return_value = proc

        await cog.self_restart(reason="test", force=True)

    execv_mock.assert_called_once()
    cog.bot.router.memory.flush.assert_called_once()


@pytest.mark.asyncio
async def test_self_restart_reaches_execv_when_memory_flush_attribute_error(cog_with_mocks):
    """memory.flush() AttributeError 不應阻斷重啟（這是昨晚 bug）。"""
    cog = cog_with_mocks
    cog.bot.router.memory.flush.side_effect = AttributeError(
        "'MemoryManager' object has no attribute '_save_data'"
    )

    with patch("os.execv") as execv_mock, \
         patch("asyncio.create_subprocess_exec") as subp_mock:
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        subp_mock.return_value = proc

        await cog.self_restart(reason="test", force=True)

    execv_mock.assert_called_once()


@pytest.mark.asyncio
async def test_self_restart_reaches_execv_when_memory_flush_generic_exception(cog_with_mocks):
    cog = cog_with_mocks
    cog.bot.router.memory.flush.side_effect = RuntimeError("disk full")

    with patch("os.execv") as execv_mock, \
         patch("asyncio.create_subprocess_exec") as subp_mock:
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        subp_mock.return_value = proc

        await cog.self_restart(reason="test", force=True)

    execv_mock.assert_called_once()


@pytest.mark.asyncio
async def test_self_restart_reaches_execv_when_bot_close_fails(cog_with_mocks):
    cog = cog_with_mocks
    cog.bot.close.side_effect = RuntimeError("connection error")

    with patch("os.execv") as execv_mock, \
         patch("asyncio.create_subprocess_exec") as subp_mock:
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        subp_mock.return_value = proc

        await cog.self_restart(reason="test", force=True)

    execv_mock.assert_called_once()


@pytest.mark.asyncio
async def test_self_restart_reaches_execv_when_git_pull_fails(cog_with_mocks):
    """git pull 失敗（網路斷、conflict 等）不應阻斷重啟。"""
    cog = cog_with_mocks

    with patch("os.execv") as execv_mock, \
         patch("asyncio.create_subprocess_exec",
               side_effect=OSError("git not found")):
        await cog.self_restart(reason="test", force=True)

    execv_mock.assert_called_once()


@pytest.mark.asyncio
async def test_self_restart_reaches_execv_when_git_pull_timeout(cog_with_mocks):
    """git pull 卡死超時不應阻斷重啟。"""
    import asyncio
    cog = cog_with_mocks

    proc = AsyncMock()

    async def _hang(*a, **kw):
        await asyncio.sleep(60)

    proc.communicate = _hang
    proc.returncode = None

    with patch("os.execv") as execv_mock, \
         patch("asyncio.create_subprocess_exec", return_value=proc), \
         patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()):
        await cog.self_restart(reason="test", force=True)

    execv_mock.assert_called_once()


@pytest.mark.asyncio
async def test_self_restart_skips_git_pull_when_pull_false(cog_with_mocks):
    cog = cog_with_mocks

    with patch("os.execv") as execv_mock, \
         patch("asyncio.create_subprocess_exec") as subp_mock:
        await cog.self_restart(reason="test", force=True, pull=False)

    execv_mock.assert_called_once()
    subp_mock.assert_not_called()


@pytest.mark.asyncio
async def test_self_restart_no_op_if_recent_restart_and_not_force(cog_with_mocks):
    """非 force 模式 + 15 分鐘內已重啟 → no-op。"""
    import time
    cog = cog_with_mocks
    cog.bot.last_restart_time = time.time()  # 剛重啟過

    with patch("os.execv") as execv_mock:
        await cog.self_restart(reason="test", force=False)

    execv_mock.assert_not_called()

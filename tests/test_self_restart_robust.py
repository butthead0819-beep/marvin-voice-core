"""TDD — self_restart 必須在所有 pre-execv 失敗下仍走到 os.execv。

歷史 bug：MemoryManager 從 JSON 重構成 SQLite 時，舊版 _save_data() 被刪除
但呼叫點沒清乾淨。導致 /marvin_reboot 噴 AttributeError 卡死沒重啟，
使用者卻以為 bot 已重啟 → 跑舊 code 卻不知道。

這組測試保證未來任何 pre-execv 步驟失敗都不會阻斷 os.execv 執行。
"""
from __future__ import annotations
import pytest
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


# ── 重啟回報：狀態檔讀寫 ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_self_restart_writes_state_file_before_execv(cog_with_mocks, tmp_path, monkeypatch):
    """self_restart 在 execv 前必須寫狀態檔到 cwd。"""
    import os as _os
    monkeypatch.chdir(tmp_path)
    from cogs.voice_controller import REBOOT_STATE_FILE
    cog = cog_with_mocks
    # 模擬 active_text_channel
    channel = MagicMock()
    channel.id = 12345
    channel.guild.id = 67890
    channel.send = AsyncMock()
    cog.active_text_channel = channel

    with patch("os.execv") as execv_mock, \
         patch("asyncio.create_subprocess_exec") as subp_mock:
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"ok", b""))
        proc.returncode = 0
        subp_mock.return_value = proc

        await cog.self_restart(reason="test", force=True, pull=True)

    # 狀態檔應該存在（execv 是 mock 過的所以不會真的替換進程）
    state_path = tmp_path / REBOOT_STATE_FILE
    assert state_path.exists(), "狀態檔應該在 execv 前寫好"

    import json as _json
    with open(state_path, encoding="utf-8") as f:
        state = _json.load(f)
    assert state["channel_id"] == 12345
    assert state["guild_id"] == 67890
    assert state["reason"] == "test"
    assert "started_at" in state
    execv_mock.assert_called_once()


@pytest.mark.asyncio
async def test_state_file_write_failure_does_not_block_execv(cog_with_mocks, monkeypatch, tmp_path):
    """狀態檔寫入失敗（disk full / 權限）也不能阻斷 execv。"""
    monkeypatch.chdir(tmp_path)
    cog = cog_with_mocks

    def _boom(*a, **kw):
        raise OSError("disk full")

    with patch("os.execv") as execv_mock, \
         patch("asyncio.create_subprocess_exec") as subp_mock, \
         patch("builtins.open", side_effect=_boom):
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        subp_mock.return_value = proc

        await cog.self_restart(reason="test", force=True, pull=False)

    execv_mock.assert_called_once()


def test_read_and_clear_reboot_state_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from cogs.voice_controller import read_and_clear_reboot_state
    assert read_and_clear_reboot_state() is None


def test_read_and_clear_reboot_state_reads_and_deletes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import json as _json
    from cogs.voice_controller import read_and_clear_reboot_state, REBOOT_STATE_FILE

    state = {"channel_id": 1, "reason": "test", "started_at": 123.0}
    with open(REBOOT_STATE_FILE, "w", encoding="utf-8") as f:
        _json.dump(state, f)

    result = read_and_clear_reboot_state()
    assert result == state
    # 讀完應該刪檔（避免下次啟動重複貼文）
    assert not (tmp_path / REBOOT_STATE_FILE).exists()


def test_read_and_clear_reboot_state_handles_corrupt_json(tmp_path, monkeypatch):
    """壞掉的 JSON 也要刪檔，避免每次啟動都嘗試讀同樣的壞檔。"""
    monkeypatch.chdir(tmp_path)
    from cogs.voice_controller import read_and_clear_reboot_state, REBOOT_STATE_FILE

    with open(REBOOT_STATE_FILE, "w", encoding="utf-8") as f:
        f.write("{ corrupt json")

    assert read_and_clear_reboot_state() is None
    assert not (tmp_path / REBOOT_STATE_FILE).exists()

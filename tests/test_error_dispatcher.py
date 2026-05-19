"""ErrorDispatcher: route Marvin's real errors to incident_writer + DM owner.

設計目標：
  - 黑名單擋掉雜訊（rtcp packets、yt-dlp deadlock、自願 restart 等 ~71/100 不該動的）
  - 白名單觸發（unhandled traceback、Tier-1 Exhausted、App Command Error、CRITICAL）
  - signature dedup（5min cooldown）避免同錯誤連噴 N 次
  - self-loop guard：dispatcher 自己 log 出來的訊息絕對不能再觸發 dispatcher
  - emit() 在 logging thread 跑，必須非阻塞 → asyncio.run_coroutine_threadsafe
  - 鑑識交給 incident_writer（純 Python，無 LLM）；DM 只是指針，Claude Code 負責處理
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from error_dispatcher import ErrorDispatcher


def _record(name: str, level: int, msg: str, exc_info=None) -> logging.LogRecord:
    return logging.LogRecord(
        name=name, level=level, pathname="x.py", lineno=1,
        msg=msg, args=(), exc_info=exc_info,
    )


@pytest.fixture
def dispatcher_factory(tmp_path):
    """造一個 dispatcher，回傳 (dispatcher, writer_mock, dm_mock, drain)。
    drain() 推進 event loop 讓 run_coroutine_threadsafe 的 task 跑完。"""
    loops_to_close = []

    def _make(cooldown=300):
        loop = asyncio.new_event_loop()
        loops_to_close.append(loop)
        # incident_writer 是同步 callable，回傳 Path
        writer_mock = MagicMock(return_value=tmp_path / "incident.md")
        dm_mock = AsyncMock()
        d = ErrorDispatcher(
            incident_writer=writer_mock,
            dm_sender=dm_mock,
            loop=loop,
            cooldown_seconds=cooldown,
        )

        def drain():
            # run_coroutine_threadsafe 透過 call_soon_threadsafe 排隊 create_task；
            # 必須先讓 loop 跑一拍才會看到 task。最多 10 拍直到沒有 pending。
            for _ in range(10):
                loop.run_until_complete(asyncio.sleep(0))
                pending = asyncio.all_tasks(loop)
                if not pending:
                    return
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))

        return d, writer_mock, dm_mock, drain

    yield _make
    for loop in loops_to_close:
        loop.close()


# ── 噪音過濾 ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,msg", [
    ("discord.ext.voice_recv.reader", "Error unpacking packet"),
    ("discord.player", "Exception in voice thread audio-player:0x113640440"),
    ("syncedlyrics", "Read timed out."),
    ("cogs.voice_controller", "❌ [Stream] yt-dlp 解析失敗: [Errno 11] Resource deadlock avoided"),
    ("cogs.voice_controller", "🚀 [Restart] 正在執行進程級重啟，原因：指揮官手動重啟"),
    ("cogs.voice_controller", "☢️ [Restart] 執行 os.execv，程序替換中..."),
    ("stt_cleaner", "[TPM Guard] Groq 清洗額度接近上限"),
])
def test_noise_filter_blocks_known_garbage(dispatcher_factory, name, msg):
    d, writer, dm, _ = dispatcher_factory()
    d.emit(_record(name, logging.ERROR, msg))
    assert writer.call_count == 0, f"噪音不應觸發 incident_writer：{name} / {msg}"
    assert dm.call_count == 0


# ── 白名單觸發 ────────────────────────────────────────────────────────────────

def test_unhandled_traceback_triggers(dispatcher_factory):
    d, writer, dm, drain = dispatcher_factory()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        exc_info = sys.exc_info()
    d.emit(_record("anywhere", logging.ERROR, "some failure", exc_info=exc_info))
    drain()
    assert writer.call_count == 1
    assert dm.call_count == 1


def test_tier1_exhausted_triggers(dispatcher_factory):
    d, writer, dm, drain = dispatcher_factory()
    d.emit(_record(
        "gemini_router_llm", logging.ERROR,
        "❌ [Tier-1 Exhausted] 雲端重試 3 次後依然失敗: 500 INTERNAL",
    ))
    drain()
    assert writer.call_count == 1


def test_app_command_error_triggers(dispatcher_factory):
    d, writer, dm, drain = dispatcher_factory()
    d.emit(_record(
        "MarvinBot", logging.ERROR,
        "❌ [App Command Error] Command 'marvin_reboot' raised an exception: AttributeError",
    ))
    drain()
    assert writer.call_count == 1


def test_critical_level_triggers_even_without_whitelist(dispatcher_factory):
    """CRITICAL 一律觸發（但自願 [Restart] 已被 noise 擋掉，這裡是其他 CRITICAL）。"""
    d, writer, dm, drain = dispatcher_factory()
    d.emit(_record("MarvinBot", logging.CRITICAL, "💀 [Sentinel] 主腦完全失聯"))
    drain()
    assert writer.call_count == 1


def test_warning_never_triggers(dispatcher_factory):
    d, writer, dm, _ = dispatcher_factory()
    d.emit(_record("anywhere", logging.WARNING, "Tier-1 Exhausted (just a warning)"))
    assert writer.call_count == 0


# ── Dedup ─────────────────────────────────────────────────────────────────────

def test_same_signature_deduped_within_cooldown(dispatcher_factory):
    d, writer, dm, drain = dispatcher_factory(cooldown=300)
    for _ in range(5):
        d.emit(_record(
            "gemini_router_llm", logging.ERROR,
            "❌ [Tier-1 Exhausted] 雲端重試 3 次後依然失敗",
        ))
    drain()
    assert writer.call_count == 1, "5 次相同錯誤應只觸發 1 次"


def test_different_signatures_not_deduped(dispatcher_factory):
    d, writer, dm, drain = dispatcher_factory()
    d.emit(_record("gemini_router_llm", logging.ERROR,
                   "❌ [Tier-1 Exhausted] 雲端重試 3 次後依然失敗"))
    d.emit(_record("MarvinBot", logging.ERROR,
                   "❌ [App Command Error] Command 'foo' raised an exception"))
    drain()
    assert writer.call_count == 2


def test_dedup_normalizes_addresses_and_long_numbers(dispatcher_factory):
    """同類錯誤但有不同 0x address / 大數字應視為同一 signature。"""
    d, writer, dm, drain = dispatcher_factory()
    d.emit(_record("MarvinBot", logging.ERROR,
                   "❌ [App Command Error] thread 0x113640440 raised exception"))
    d.emit(_record("MarvinBot", logging.ERROR,
                   "❌ [App Command Error] thread 0x55adbeef raised exception"))
    drain()
    assert writer.call_count == 1


def test_dedup_expires_after_cooldown(dispatcher_factory):
    d, writer, dm, drain = dispatcher_factory(cooldown=300)
    d.emit(_record("gemini_router_llm", logging.ERROR,
                   "❌ [Tier-1 Exhausted] 雲端重試 3 次後依然失敗"))
    drain()
    # 偽造時鐘跳到 cooldown 之後
    d._last_seen = {sig: ts - 400 for sig, ts in d._last_seen.items()}
    d.emit(_record("gemini_router_llm", logging.ERROR,
                   "❌ [Tier-1 Exhausted] 雲端重試 3 次後依然失敗"))
    drain()
    assert writer.call_count == 2


# ── Self-loop guard ───────────────────────────────────────────────────────────

def test_dispatcher_own_logger_never_triggers(dispatcher_factory):
    """dispatcher 自己 log 的 ERROR 絕對不能再觸發 dispatcher（避免無窮迴圈）。"""
    d, writer, dm, _ = dispatcher_factory()
    d.emit(_record("error_dispatcher", logging.ERROR,
                   "incident write failed"))
    d.emit(_record("error_dispatcher.foo", logging.CRITICAL,
                   "anything"))
    assert writer.call_count == 0


def test_writer_timeout_does_not_permanently_block_dispatcher(dispatcher_factory):
    """writer 卡超過 timeout → dispatcher 必須超時中止，且 _inflight 被釋放，後續錯誤仍可派發。"""
    import time as _time
    d, writer, dm, drain = dispatcher_factory()
    d.writer_timeout_seconds = 0.1  # 縮短 timeout 以便測試

    # 第一次：writer 故意 hang
    def _hang(_rec, _rec24h):
        _time.sleep(2.0)
        return "should-never-return"
    writer.side_effect = _hang

    d.emit(_record("MarvinBot", logging.CRITICAL, "first failure"))
    drain()
    # 第一次 DM 應出現「超時 / timeout」字樣
    assert dm.call_count == 1
    first_dm = dm.call_args_list[0][0][0]
    assert "timeout" in first_dm.lower() or "超時" in first_dm

    # _inflight 必須已釋放
    assert d._inflight is False, "writer timeout 後 _inflight 必須清除，否則後續錯誤永久 block"

    # 第二次：用不同 signature（不同 logger）避免 dedup
    writer.side_effect = None
    writer.return_value = __import__("pathlib").Path("/tmp/ok.md")
    d.emit(_record("gemini_router_llm", logging.ERROR,
                   "❌ [Tier-1 Exhausted] dummy"))
    drain()
    assert dm.call_count == 2, "writer 解除卡死後，新錯誤應正常派發"


def test_writer_failure_still_sends_fallback_dm(dispatcher_factory):
    """incident_writer raise 不能讓 dispatcher 自己 re-enter，且 DM 仍要送 fallback。"""
    d, writer, dm, drain = dispatcher_factory()
    writer.side_effect = RuntimeError("disk full")

    d.emit(_record("MarvinBot", logging.CRITICAL, "💀 主腦失聯"))
    drain()
    # 即使 writer failed，dm_sender 仍應收到 fallback 訊息
    assert dm.call_count == 1
    sent = dm.call_args[0][0]
    assert ("disk full" in sent
            or "report failed" in sent.lower()
            or "incident_writer" in sent)


# ── 傳遞 LogRecord 正確 ───────────────────────────────────────────────────────

def test_writer_receives_actual_log_record(dispatcher_factory):
    """ErrorDispatcher 必須把 LogRecord 物件原封傳給 incident_writer，不要轉成字串。"""
    d, writer, dm, drain = dispatcher_factory()
    try:
        raise KeyError("missing_key")
    except KeyError:
        import sys
        exc_info = sys.exc_info()
    rec = _record("some.module", logging.ERROR,
                  "specific failure message", exc_info=exc_info)
    d.emit(rec)
    drain()
    # writer 收到 (record, recurrence_24h)
    assert writer.call_count == 1
    args, _kwargs = writer.call_args
    passed_record = args[0]
    recurrence = args[1]
    assert isinstance(passed_record, logging.LogRecord)
    assert passed_record.name == "some.module"
    assert passed_record.getMessage() == "specific failure message"
    assert passed_record.exc_info is not None
    assert isinstance(recurrence, int) and recurrence >= 1


def test_recurrence_count_grows_with_repeats(dispatcher_factory):
    """同 signature 連續觸發時，writer 收到的 recurrence_24h 應遞增（即便 dedup 沒讓它每次都跑）。"""
    # 用 cooldown=0 讓每次都觸發 writer，純驗證計數
    d, writer, dm, drain = dispatcher_factory(cooldown=0)
    for _ in range(3):
        d.emit(_record(
            "gemini_router_llm", logging.ERROR,
            "❌ [Tier-1 Exhausted] 雲端重試 3 次後依然失敗",
        ))
    drain()
    recurrences = [call.args[1] for call in writer.call_args_list]
    assert recurrences == [1, 2, 3]


def test_dm_summary_contains_incident_path(dispatcher_factory, tmp_path):
    """DM 訊息要包含 incident 檔的路徑，方便 Claude Code 抓。"""
    d, writer, dm, drain = dispatcher_factory()
    writer.return_value = tmp_path / "2026-05-18-074400-some-incident.md"
    d.emit(_record("gemini_router_llm", logging.ERROR,
                   "❌ [Tier-1 Exhausted] dummy"))
    drain()
    sent = dm.call_args[0][0]
    assert "2026-05-18-074400-some-incident.md" in sent

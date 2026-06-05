"""TDD — pipeline_timing 在 async + create_task 邊界內正確記錄階段時間戳。"""
from __future__ import annotations

import asyncio
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

# Make project root importable
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_emit_silent_without_start():
    """沒呼叫 start() → emit 不該炸也不該印任何東西"""
    import pipeline_timing
    buf = io.StringIO()
    with redirect_stdout(buf):
        pipeline_timing.emit("alice", "hello")
    assert buf.getvalue() == "", f"expected silent, got: {buf.getvalue()!r}"


def test_mark_silent_without_start():
    """同上：mark 在沒 start 的情況也安靜"""
    import pipeline_timing
    # 不該丟例外
    pipeline_timing.mark("stt_done")


def test_start_mark_emit_basic():
    import pipeline_timing

    pipeline_timing.start()
    pipeline_timing.mark("stt_start")
    pipeline_timing.mark("stt_done")
    pipeline_timing.mark("cleaner_done")
    pipeline_timing.mark("intent_dispatched")

    buf = io.StringIO()
    with redirect_stdout(buf):
        pipeline_timing.emit("狗與露", "播放周杰倫的稻香")
    out = buf.getvalue()
    assert "[STAGE_TIMING]" in out
    assert "speaker=狗與露" in out
    assert "sttstart=" in out
    assert "sttdone=" in out
    assert "cleanerdone=" in out
    assert "intentdispatched=" in out
    assert "total=" in out
    assert "text='播放周杰倫的稻香'" in out


@pytest.mark.asyncio
async def test_context_propagates_across_create_task():
    """關鍵驗項：start 在 async 框架，create_task 出去的 sub-task 該看得到同份 dict。"""
    import pipeline_timing

    pipeline_timing.start()
    pipeline_timing.mark("stt_start")

    async def downstream():
        pipeline_timing.mark("stt_done")
        await asyncio.sleep(0.001)
        pipeline_timing.mark("cleaner_done")

    await asyncio.create_task(downstream())
    pipeline_timing.mark("intent_dispatched")

    snap = pipeline_timing.snapshot()
    assert snap is not None
    assert {"endpoint", "stt_start", "stt_done", "cleaner_done", "intent_dispatched"} <= snap.keys(), snap


@pytest.mark.asyncio
async def test_isolated_per_async_task():
    """兩個獨立 task 各自 start，state 不該交叉污染。"""
    import pipeline_timing

    results = {}

    async def task_a():
        pipeline_timing.start()
        pipeline_timing.mark("stt_done")
        await asyncio.sleep(0.01)
        results["a"] = pipeline_timing.snapshot()

    async def task_b():
        pipeline_timing.start()
        pipeline_timing.mark("cleaner_done")
        await asyncio.sleep(0.01)
        results["b"] = pipeline_timing.snapshot()

    await asyncio.gather(task_a(), task_b())

    assert results["a"] is not None
    assert results["b"] is not None
    assert "stt_done" in results["a"] and "cleaner_done" not in results["a"]
    assert "cleaner_done" in results["b"] and "stt_done" not in results["b"]


def test_emit_includes_queue_and_question_stages():
    """新增中間打點 dequeued / question_done → emit 行要帶 dequeued= / questiondone=，
    讓 analyzer 能把舊的混合 cleaner 段拆成排隊 / 等問句 / 真清洗。"""
    import pipeline_timing

    pipeline_timing.start()
    pipeline_timing.mark("stt_start")
    pipeline_timing.mark("stt_done")
    pipeline_timing.mark("dequeued")
    pipeline_timing.mark("question_done")
    pipeline_timing.mark("cleaner_done")
    pipeline_timing.mark("intent_dispatched")

    buf = io.StringIO()
    with redirect_stdout(buf):
        pipeline_timing.emit("狗與露", "馬文的厭世日記")
    out = buf.getvalue()
    assert "dequeued=" in out, out
    assert "questiondone=" in out, out


def test_emit_partial_stages_ok():
    """只跑到一半就 emit 也該正常輸出已記錄的，不該 KeyError。"""
    import pipeline_timing

    pipeline_timing.start()
    pipeline_timing.mark("stt_done")  # 跳過 stt_start
    buf = io.StringIO()
    with redirect_stdout(buf):
        pipeline_timing.emit("bob", "hi")
    out = buf.getvalue()
    assert "[STAGE_TIMING]" in out
    assert "sttdone=" in out
    assert "sttstart=" not in out
    assert "intentdispatched=" not in out


@pytest.mark.asyncio
async def test_restore_carries_timing_across_queue_boundary():
    """跨 asyncio.Queue 邊界：producer snapshot → 把 dict 塞進 queue item →
    consumer 從 queue 拿到後 restore → 後續 mark/emit 看得到 producer 的 endpoint。

    這是 STAGE_TIMING 真正能跑的修法：ContextVar 不會自動跨 asyncio.Queue 邊界，
    必須手動 forward。
    """
    import asyncio
    import pipeline_timing

    q: asyncio.Queue = asyncio.Queue()

    async def producer():
        pipeline_timing.start()
        pipeline_timing.mark("stt_start")
        pipeline_timing.mark("stt_done")
        snap = pipeline_timing.snapshot()
        await q.put({"payload": "hello", "_timing": snap})

    async def consumer():
        item = await q.get()
        pipeline_timing.restore(item["_timing"])
        pipeline_timing.mark("intent_dispatched")
        buf = io.StringIO()
        with redirect_stdout(buf):
            pipeline_timing.emit("alice", item["payload"])
        return buf.getvalue()

    await producer()
    out = await consumer()
    assert "[STAGE_TIMING]" in out, f"emit silent — restore 沒生效: {out!r}"
    assert "sttstart=" in out, "producer side mark 沒過 queue"
    assert "sttdone=" in out
    assert "intentdispatched=" in out, "consumer side mark 沒打進來"


def test_restore_with_none_is_noop():
    """consumer 沒拿到 timing dict 也別炸 — 老 queue item 沒 _timing 是常態。

    用 contextvars.Context 強制隔離 — pytest 同 process 跨 test 共享 root context，
    前一個 test 留下的 ContextVar 會污染。
    """
    import contextvars
    import pipeline_timing

    result = {}

    def isolated():
        pipeline_timing.restore(None)
        result["snap"] = pipeline_timing.snapshot()

    contextvars.Context().run(isolated)
    assert result["snap"] is None, "restore(None) 不該建出空 dict"

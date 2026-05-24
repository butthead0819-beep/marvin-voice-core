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

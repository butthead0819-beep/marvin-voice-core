"""TDD: SpeakerDispatcher — per-speaker 序列化（方案A，根治 STT 延遲尾巴）。

不變量：
  I1 同 speaker 嚴格 FIFO 序列（保護 per-speaker state：dialogue_states/
     speech_buffers 本就 keyed，同 speaker 序列即安全）
  I2 跨 speaker 並行（A 的等問句/cleaner 不再讓 B 陪等——7/2 實測
     queue_wait 14-27s 佔總延遲 74-90% 的根治）
  I3 handler 崩潰隔離（一項炸不掉 worker、不影響其他 speaker）
  I4 佇列深度上限（drop-oldest，防單人 retry 風暴堆積死查詢）
  I5 閒置回收（speaker 離場後 worker 自動退場，不洩漏 task）
"""
from __future__ import annotations

import asyncio

import pytest

from speaker_dispatch import SpeakerDispatcher


@pytest.mark.asyncio
async def test_same_speaker_strict_fifo():
    done = []

    async def handler(item):
        await asyncio.sleep(0.01)
        done.append(item["n"])

    d = SpeakerDispatcher(handler)
    for n in range(5):
        d.submit("阿明", {"n": n})
    await d.drain()
    assert done == [0, 1, 2, 3, 4]


@pytest.mark.asyncio
async def test_cross_speaker_parallel():
    """A 的慢 handler 不得阻塞 B——B 必須在 A 完成前先完成。"""
    order = []
    a_started = asyncio.Event()

    async def handler(item):
        if item["who"] == "A":
            a_started.set()
            await asyncio.sleep(0.3)
        order.append(item["who"])

    d = SpeakerDispatcher(handler)
    d.submit("A", {"who": "A"})
    await a_started.wait()
    d.submit("B", {"who": "B"})
    await d.drain()
    assert order == ["B", "A"]   # B 沒有陪 A 等


@pytest.mark.asyncio
async def test_handler_crash_does_not_kill_worker():
    done = []

    async def handler(item):
        if item["n"] == 0:
            raise RuntimeError("boom")
        done.append(item["n"])

    d = SpeakerDispatcher(handler)
    d.submit("阿明", {"n": 0})
    d.submit("阿明", {"n": 1})
    await d.drain()
    assert done == [1]


@pytest.mark.asyncio
async def test_depth_cap_drops_oldest():
    done = []
    gate = asyncio.Event()

    async def handler(item):
        await gate.wait()
        done.append(item["n"])

    d = SpeakerDispatcher(handler, max_depth=3)
    for n in range(6):   # n=0 立刻被 worker 取走卡在 gate；1-5 進佇列，cap=3
        d.submit("阿明", {"n": n})
        await asyncio.sleep(0)   # 讓 worker 有機會取走第一項
    gate.set()
    await d.drain()
    assert done[0] == 0            # 處理中的不受影響
    assert done[-1] == 5           # 最新的保留
    assert len(done) <= 4          # 0 + cap 3 = 至多 4 項


@pytest.mark.asyncio
async def test_idle_worker_reaped_and_resubmit_works():
    done = []

    async def handler(item):
        done.append(item["n"])

    d = SpeakerDispatcher(handler, idle_ttl_s=0.05)
    d.submit("阿明", {"n": 1})
    await d.drain()
    await asyncio.sleep(0.15)      # 超過 idle_ttl → worker 退場
    assert d.active_workers == 0
    d.submit("阿明", {"n": 2})     # 重新提交 → 自動起新 worker
    await d.drain()
    assert done == [1, 2]


@pytest.mark.asyncio
async def test_pending_counts_per_speaker():
    gate = asyncio.Event()

    async def handler(item):
        await gate.wait()

    d = SpeakerDispatcher(handler)
    d.submit("阿明", {})
    await asyncio.sleep(0)
    d.submit("阿明", {})
    d.submit("狗與露", {})
    assert d.pending("阿明") >= 1     # 一項處理中、一項排隊
    assert d.pending("路人") == 0
    gate.set()
    await d.drain()


@pytest.mark.asyncio
async def test_shutdown_cancels_workers():
    async def handler(item):
        await asyncio.sleep(10)

    d = SpeakerDispatcher(handler)
    d.submit("阿明", {})
    await asyncio.sleep(0)
    assert d.active_workers == 1
    await d.shutdown()
    assert d.active_workers == 0

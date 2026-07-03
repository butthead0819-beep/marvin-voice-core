"""SpeakerDispatcher — per-speaker 序列化佇列（方案A，根治 STT 延遲尾巴）。

背景（memory stt_queue_tail_single_worker）：原單一 query_queue + 唯一
worker while-loop，worker 內等問句(4s)/cleaner(2.5s)/dispatch(~10s) 卡住
後面全陪等——7/2 實測 queue_wait 14-27s、佔總延遲 74-90%，且延遲引發
使用者重試形成惡性循環。

設計：每個 speaker 一條 lazy queue + 專屬 worker task——
  - 同 speaker 嚴格 FIFO（per-speaker state：dialogue_states/speech_buffers
    本就 keyed，同 speaker 序列即安全，這正是原單線刻意保護的不變量）
  - 跨 speaker 並行（A 的等問句不再讓 B 陪等）
  - handler 例外逐項吞（崩潰隔離）、深度上限 drop-oldest（retry 風暴不堆死查詢）、
    閒置 TTL 自動回收 worker（離場不洩漏 task）

kill-switch：voice_controller 端 env MARVIN_PER_SPEAKER_QUEUE（run_bot 顯式開；
0 → 走原單 worker legacy 路，此模組完全不啟用）。
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

DEFAULT_MAX_DEPTH = 8       # 單人佇列上限；超過丟最舊（stale drop 在 handler 內另有一道）
DEFAULT_IDLE_TTL_S = 900.0  # worker 閒置 15 分鐘自動退場


class SpeakerDispatcher:
    def __init__(self, handler, *, max_depth: int = DEFAULT_MAX_DEPTH,
                 idle_ttl_s: float = DEFAULT_IDLE_TTL_S, name: str = "SpeakerDispatch"):
        self._handler = handler
        self._max_depth = max_depth
        self._idle_ttl_s = idle_ttl_s
        self._name = name
        self._queues: dict[str, asyncio.Queue] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._inflight: dict[str, bool] = {}
        # 開機可驗證（J2 空轉教訓：wire ≠ 啟用）；WARNING 級確保 root logger 放行
        logger.warning(f"🚀 [{name}] per-speaker 序列化啟用（depth={max_depth}, idle_ttl={idle_ttl_s:.0f}s）")

    # ── API ──────────────────────────────────────────────────────────────

    def submit(self, key: str, item) -> None:
        """排入該 speaker 的序列佇列；worker 不在就起一條。sync、不阻塞。"""
        q = self._queues.get(key)
        if q is None:
            q = self._queues[key] = asyncio.Queue()
        if q.qsize() >= self._max_depth:
            try:
                dropped = q.get_nowait()
                q.task_done()
                logger.warning(f"⚠️ [{self._name}] {key} 佇列滿({self._max_depth})，"
                               f"丟最舊一項（retry 風暴保護）")
                del dropped
            except asyncio.QueueEmpty:
                pass
        q.put_nowait(item)
        w = self._workers.get(key)
        if w is None or w.done():
            self._workers[key] = asyncio.get_running_loop().create_task(
                self._worker(key), name=f"{self._name}:{key}")

    def pending(self, key: str) -> int:
        """該 speaker 排隊中+處理中的項數（0=閒）。"""
        q = self._queues.get(key)
        n = q.qsize() if q else 0
        return n + (1 if self._inflight.get(key) else 0)

    @property
    def active_workers(self) -> int:
        return sum(1 for w in self._workers.values() if not w.done())

    async def drain(self) -> None:
        """等所有佇列清空+處理完（測試/優雅關機用）。"""
        while any(q.qsize() for q in self._queues.values()) or any(self._inflight.values()):
            await asyncio.sleep(0.01)

    async def shutdown(self) -> None:
        for w in self._workers.values():
            w.cancel()
        await asyncio.gather(*self._workers.values(), return_exceptions=True)
        self._workers.clear()
        self._inflight.clear()

    # ── worker ───────────────────────────────────────────────────────────

    async def _worker(self, key: str) -> None:
        q = self._queues[key]
        while True:
            try:
                item = await asyncio.wait_for(q.get(), timeout=self._idle_ttl_s)
            except asyncio.TimeoutError:
                # 閒置回收：清掉自己（speaker 離場後不留殭屍 task）
                self._workers.pop(key, None)
                self._queues.pop(key, None)
                return
            except asyncio.CancelledError:
                return
            self._inflight[key] = True
            try:
                await self._handler(item)
            except asyncio.CancelledError:
                self._inflight[key] = False
                q.task_done()
                return
            except Exception:
                logger.exception(f"❌ [{self._name}] {key} handler 例外（隔離，worker 續跑）")
            finally:
                self._inflight[key] = False
                q.task_done()

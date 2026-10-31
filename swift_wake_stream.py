"""SwiftWakeStreamDetector — WakeStreamDetector 的常駐 Swift SpeechAnalyzer 替代品。

跟 wake_stream_detector.WakeStreamDetector 同介面（on_speech_start / push_pcm /
on_speech_end / rearm / cleanup），讓 PuckMicSession 可以切換用哪一個。只支援
16k mono 單一 user（puck 路徑固定用 user_id=0）。

跟常駐的 macos_stt_stream_bin 子程序講二進位 framing（見 macos_stt_stream.swift
檔頭協定）；子程序跟 session 同生命週期，掛了就帶 backoff 自動重啟，重啟後時間軸
（去重狀態）歸零——新程序的 CMTime 從 0 開始算。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
from pathlib import Path
from typing import Callable, Optional

from puck_wake import pinyin_wake_match

logger = logging.getLogger(__name__)

_DEFAULT_BIN = str(Path(__file__).resolve().parent / "macos_stt_stream_bin")


class SwiftWakeStreamDetector:
    def __init__(self, on_wake_callback, loop: asyncio.AbstractEventLoop, *,
                 cmd: Optional[list[str]] = None,
                 wake_matcher: Callable[[str], Optional[int]] = pinyin_wake_match,
                 context_strings: Optional[str] = None,
                 log_text: bool = False,
                 restart_backoff_s: float = 1.0,
                 max_backoff_s: float = 30.0,
                 max_write_buffer: int = 1 << 20):
        self._on_wake_callback = on_wake_callback
        self._loop = loop
        self._cmd = cmd if cmd is not None else [_DEFAULT_BIN]
        self._wake_matcher = wake_matcher
        self._context_strings = context_strings
        self._log_text = log_text
        self._restart_backoff_s = restart_backoff_s
        self._max_backoff_s = max_backoff_s
        self._max_write_buffer = max_write_buffer

        self._backoff = restart_backoff_s
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._closing = False

        self._ready = False
        self._armed = False
        self._fired = False
        self._reset_dedupe()
        self._first_audio_time = 0.0
        self.last_wake_t: Optional[float] = None

        self._not_ready_drop_logged = False
        self._bad_json_count = 0
        self._results = 0
        self._fires = 0
        self._restarts = 0
        self._dropped_writes = 0

        self._run_task = loop.create_task(self._run())

    # ── Public API（跟 WakeStreamDetector 同介面） ────────────────────────────

    def on_speech_start(self, user_id: int, first_audio_time: float, pre_roll: bytes = b"") -> None:
        self._armed = True
        self._first_audio_time = first_audio_time
        if pre_roll and self._ready:
            self._write_frame(b"A", pre_roll)

    def push_pcm(self, user_id: int, pcm: bytes) -> None:
        if not self._armed or self._fired:
            return
        if not self._ready:
            if not self._not_ready_drop_logged:
                logger.info("ℹ️ [SwiftWake] 子程序尚未 ready，丟棄音訊")
                self._not_ready_drop_logged = True
            return
        if len(pcm) > self._max_write_buffer:
            pass  # 單塊超過上限也直接嘗試寫，交給下面 buffer size 檢查判斷
        self._write_frame(b"A", pcm)

    def on_speech_end(self, user_id: int) -> None:
        self._armed = False
        if self._ready:
            self._write_frame(b"F")

    def rearm(self, user_id) -> None:
        self._fired = False

    def cleanup(self) -> None:
        self._armed = False
        self._fired = False

    async def aclose(self) -> None:
        self._closing = True
        if self._proc is not None and self._proc.stdin is not None:
            try:
                self._proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
        if self._proc is not None:
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                try:
                    self._proc.kill()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await self._proc.wait()
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                pass
        if self._stderr_task is not None:
            self._stderr_task.cancel()
        if self._run_task is not None:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def stats(self) -> dict:
        return {
            "results": self._results,
            "fires": self._fires,
            "restarts": self._restarts,
            "dropped_writes": self._dropped_writes,
        }

    # ── 子程序管理 ──────────────────────────────────────────────────────────

    async def _run(self) -> None:
        first = True
        while not self._closing:
            if not first:
                self._restarts += 1
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, self._max_backoff_s)
                if self._closing:
                    break
            first = False

            env = dict(os.environ)
            if self._context_strings:
                env["STT_CONTEXT_STRINGS"] = self._context_strings

            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *self._cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"⚠️ [SwiftWake] 啟動子程序失敗: {e}")
                continue

            self._ready = False
            self._reset_dedupe()

            self._stderr_task = self._loop.create_task(self._pump_stderr(self._proc))
            try:
                await self._read_stdout(self._proc)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"⚠️ [SwiftWake] stdout 讀取例外: {e}")
            finally:
                self._stderr_task.cancel()

            self._ready = False
            self._reset_dedupe()

            try:
                await asyncio.wait_for(self._proc.wait(), timeout=1.0)
            except Exception:  # noqa: BLE001
                pass

            if self._closing:
                break
            logger.warning(f"⚠️ [SwiftWake] 子程序結束，{self._backoff:.1f}s 後重啟")

    async def _pump_stderr(self, proc: asyncio.subprocess.Process) -> None:
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                logger.debug(f"[SwiftWake stderr] {line.decode(errors='replace').rstrip()}")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass

    async def _read_stdout(self, proc: asyncio.subprocess.Process) -> None:
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._bad_json_count += 1
                if self._bad_json_count % 50 == 1:
                    logger.warning(f"⚠️ [SwiftWake] 壞 JSON 行，累計 {self._bad_json_count}")
                continue

            kind = obj.get("type")
            if kind == "ready":
                self._ready = True
                self._backoff = self._restart_backoff_s
                logger.info("✅ [SwiftWake] 子程序 ready")
            elif kind == "result":
                self._results += 1
                await self._handle_result(obj)

    def _reset_dedupe(self) -> None:
        # 新程序時間軸從 0 開始，去重狀態一起歸零
        self._range_start = None
        self._consumed = 0
        self._last_fire_end = float("-inf")

    # ── 寫入 ────────────────────────────────────────────────────────────────

    def _write_frame(self, type_byte: bytes, payload: bytes = b"") -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        try:
            transport = self._proc.stdin.transport
            if transport is not None and transport.get_write_buffer_size() > self._max_write_buffer:
                self._dropped_writes += 1
                if self._dropped_writes % 50 == 1:
                    logger.warning(f"⚠️ [SwiftWake] 寫入緩衝區過大，丟棄音訊塊（累計 {self._dropped_writes}）")
                return
            self._proc.stdin.write(struct.pack("<cI", type_byte, len(payload)) + payload)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[SwiftWake] 寫入失敗: {e}")

    # ── result 處理 ─────────────────────────────────────────────────────────

    def _count_wakes(self, text: str) -> int:
        n = 0
        while True:
            end = self._wake_matcher(text)
            if end is None or end <= 0:
                return n
            n += 1
            text = text[end:]

    async def _handle_result(self, obj: dict) -> None:
        # 去重（2026-10-31 實測 SpeechAnalyzer 行為）：volatile result 只有一個 run、時間＝
        # 整段未定稿範圍 [start, 目前餵到的位置]，沒有逐字時間；final 才有。所以改用
        # 「同一個 range start 底下喚醒詞出現次數」去重：次數超過已用掉的才觸發。
        # 新 range 的 start 早於上次觸發時聽到的位置（final 把舊範圍切開、或改寫）→
        # 裡面已有的喚醒視為用掉，只有後來新增的才算。
        text = obj.get("text", "") or ""
        start = obj.get("start")
        end = obj.get("end")
        count = self._count_wakes(text)

        if start != self._range_start:
            self._range_start = start
            stale = start is None or start < self._last_fire_end
            self._consumed = count if stale else 0

        if self._fired:
            self._consumed = max(self._consumed, count)
            return
        if count <= self._consumed:
            return

        self._consumed = count
        self._fired = True
        self._last_fire_end = end if end is not None else self._last_fire_end
        self.last_wake_t = end
        self._fires += 1

        if self._log_text:
            logger.info(f"🎯 [SwiftWake] User_0 → '{text}'")
        else:
            logger.info("🎯 [SwiftWake] User_0 候選命中")

        try:
            await self._on_wake_callback(0, self._first_audio_time, text)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"⚠️ [SwiftWake] on_wake_callback 例外: {e}")

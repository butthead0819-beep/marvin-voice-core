"""HotSwapCoordinator — 中途插 TTS 熱切換的狀態機。

純 logic（無 IO、無 discord），voice_controller 的播放迴圈用它決定何時 stop→play
切換，after_callback 用 `is_swapping` 區分「主動切換的 stop」vs「歌自然結束」。

狀態：
  idle      無 pending target
  armed     有 target + 等 stream2 ready
  ready     target 到 + stream2 好 → 可切
  swapping  begin_swap() 後、finish_swap() 前

刻意 fail-safe：stream2 未 ready 絕不回 True（寧可放棄插話也不切到空源）。
"""
from __future__ import annotations

from typing import Any


class HotSwapCoordinator:
    def __init__(self) -> None:
        self._target: float | None = None
        self._stream2: Any | None = None
        self._swapping: bool = False

    def request(self, target_seconds: float) -> None:
        """登記一次插話：在 target_seconds 切換。覆蓋前一個未觸發的 request。"""
        self._target = target_seconds
        self._stream2 = None
        self._swapping = False

    def set_stream2_ready(self, source: Any) -> None:
        """背景備好的 stream2 source 就緒。"""
        self._stream2 = source

    def ready_to_swap(self, current_position: float) -> bool:
        return (
            not self._swapping
            and self._target is not None
            and self._stream2 is not None
            and current_position >= self._target
        )

    def begin_swap(self) -> Any:
        """進切換：回 stream2 source，標記 swapping（after_callback 據此判斷）。"""
        self._swapping = True
        return self._stream2

    def finish_swap(self) -> None:
        """切換完成，stream2 成為新當前播放，回 idle。"""
        self._target = None
        self._stream2 = None
        self._swapping = False

    def abort(self) -> None:
        """放棄這次插話（備料趕不上 / 取消），清 pending，不影響當前播放。"""
        self._target = None
        self._stream2 = None
        self._swapping = False

    @property
    def is_swapping(self) -> bool:
        return self._swapping

    @property
    def is_busy(self) -> bool:
        """有 pending target 或正在 swapping → 不能再 arm 新插話（音量 swap 據此排隊）。"""
        return self._target is not None or self._swapping

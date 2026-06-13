"""語意斷句決策核心（Volatile Phase 1，2026-06-13 hot sprint）。

把 volatile 文字流轉成「何時提前切句」：文字穩定 stability_window 毫秒即視為
講者講完，不必等滿 VAD 純靜默 0.8-3s。純函式狀態機，無 I/O，可單測。

無翻盤率數據的減災（hot sprint，先 OFF 上線後 A/B）：
- revision（改寫非延伸）重置穩定窗 → 模型還不確定時自動往後延切點
- 穩定窗有下限 _FLOOR_MS、min 語句長度，短雜訊碎片不亂切
- 穩定窗吃對話溫度（沿 VAD 既有 high/mid/low 語意）
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_FLOOR_MS = 500           # 穩定窗下限（保守保護，永不低於此）
_PUNCT_RE = re.compile(r"[\s，。！？、；,.!?]+")

# 對話溫度 → 穩定窗（對齊 VAD 截斷靜默 high 3.0/mid 1.5/low 0.8 的相對關係，但壓在更短
# 區間——語意斷句靠「文字穩定」這個比純靜默強的訊號，可以比 VAD 更早切）
_TEMP_WINDOW_MS = {"high": 1200, "mid": 800, "low": 500}


@dataclass(frozen=True)
class CutDecision:
    text: str            # 切句當下的最終文字
    cut_ms: int          # 決定切的時間戳（餵入的 t_ms）
    revision_count: int  # 此語句累積改寫次數（投機風險指標）


def _norm(text: str) -> str:
    """去標點空格，用於判斷「文字內容」是否真的變了。"""
    return _PUNCT_RE.sub("", text)


class SemanticEndpointer:
    def __init__(self, *, stability_window_ms: int = 800, min_duration_ms: int = 300):
        self._stability_window_ms = int(stability_window_ms)
        self._min_duration_ms = int(min_duration_ms)
        self.reset()

    @classmethod
    def from_temperature(cls, temp: str, *, min_duration_ms: int = 300) -> "SemanticEndpointer":
        # floor 只在 live 溫度路徑套（測試可用顯式小窗驗邏輯）
        window = max(_FLOOR_MS, _TEMP_WINDOW_MS.get(temp, _TEMP_WINDOW_MS["mid"]))
        return cls(stability_window_ms=window, min_duration_ms=min_duration_ms)

    def reset(self) -> None:
        self._first_ms: int | None = None
        self._last_change_ms: int | None = None
        self._last_norm: str = ""
        self._revisions: int = 0
        self._fired: bool = False

    def observe(self, t_ms: int, text: str) -> CutDecision | None:
        """餵一筆 volatile 更新。回 CutDecision 表示「現在切」，否則 None。

        切句後再餵會回 None（idempotent，直到 reset）。
        """
        if self._fired:
            return None
        norm = _norm(text)
        if not norm:
            return None

        if self._first_ms is None:
            self._first_ms = t_ms

        if norm != self._last_norm:
            if self._last_norm and not norm.startswith(self._last_norm):
                self._revisions += 1   # 改寫（非延伸）
            self._last_norm = norm
            self._last_change_ms = t_ms

        assert self._last_change_ms is not None and self._first_ms is not None
        stable_for = t_ms - self._last_change_ms
        total = t_ms - self._first_ms
        if stable_for >= self._stability_window_ms and total >= self._min_duration_ms:
            self._fired = True
            return CutDecision(text=text, cut_ms=t_ms, revision_count=self._revisions)
        return None

"""Tier 3 付費 LLM spending guard（Plan B 第三層）。

Gemini 2.5 Pro（daily review 深度分析）是唯一付費 tier。痛點：cost 不可見、撞 cap
沒人知道（事後才從帳單看到）。本模組：估每次 cost、寫 records/llm_paid_usage.jsonl、
enforce daily/monthly USD cap。

Tier3 政策（per project_llm_tier_wrapper）：不 fallback（用就是要品質）；超 cap 直接拒
（raise / caller 自處），不靜默降級。est_usd 保守略高——cap 是守門，非精算帳單。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

DEFAULT_PAID_LOG = Path("records/llm_paid_usage.jsonl")

# 粗估單價（USD / 1M tokens）：(input, output)。保守略高。
_PRICE_PER_1M: dict[str, tuple[float, float]] = {
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash": (0.30, 2.50),
    # flash-lite-preview：Marvin reply paid fallback 用。略保守。
    "gemini-3.1-flash-lite": (0.10, 0.40),
}
_DEFAULT_PRICE = (2.0, 12.0)


def estimate_cost(model: str, in_tokens: int, out_tokens: int) -> float:
    """粗估 USD。prefix 比對（容忍版本後綴如 -preview-05-20）；未知 model 用保守預設。"""
    pin, pout = _DEFAULT_PRICE
    m = model or ""
    for key in sorted(_PRICE_PER_1M, key=len, reverse=True):  # 最長前綴優先
        if m.startswith(key):
            pin, pout = _PRICE_PER_1M[key]
            break
    return (max(0, in_tokens) * pin + max(0, out_tokens) * pout) / 1_000_000


class PaidSpendingExceeded(Exception):
    """超出 daily/monthly USD cap。caller 應跳過該次付費呼叫。"""


@dataclass
class PaidUsageGuard:
    # 2026-07-03 收緊：GCP spending cap 只有 $10（使用者訂）。daily 0.5 防
    # runaway 輸出型事故一天燒掉月預算；monthly 4.0 → 最壞 $10 撐 2.5 個月、
    # 典型月（~$2）撐 4-5 個月。實際 30 天帳單 $2.65（diary_comic 佔 2/3）。
    log_path: Path = DEFAULT_PAID_LOG
    daily_cap_usd: float = 0.5
    monthly_cap_usd: float = 4.0
    clock: Callable[[], float] = time.time

    def _rows(self) -> list[dict]:
        p = Path(self.log_path)
        if not p.exists():
            return []
        rows: list[dict] = []
        try:
            for line in p.open(encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    continue  # 壞行跳過，不毀整檔
        except Exception:
            return rows
        return rows

    def _spent_since(self, since_ts: float) -> float:
        return sum(float(r.get("est_usd", 0) or 0) for r in self._rows()
                   if float(r.get("ts", 0) or 0) >= since_ts)

    def spent_today(self) -> float:
        now = self.clock()
        start = datetime.fromtimestamp(now).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        return self._spent_since(start)

    def spent_month(self) -> float:
        now = self.clock()
        start = datetime.fromtimestamp(now).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        return self._spent_since(start)

    def allow(self, expected_usd: float) -> bool:
        """這次預估花費加進去後仍在 daily 且 monthly cap 內 → True。"""
        return (self.spent_today() + expected_usd <= self.daily_cap_usd
                and self.spent_month() + expected_usd <= self.monthly_cap_usd)

    def record(self, *, caller: str, model: str, tokens: int, est_usd: float,
               in_tokens: int | None = None, out_tokens: int | None = None) -> None:
        """付費呼叫成功後寫一行（永不 raise；IO 失敗只略過）。

        in/out 分開存（2026-07-03）：output 單價是 input 的 8 倍，混計無法
        診斷「誰在生成大量輸出」。舊 caller 不帶 split 仍相容（不寫該欄）。
        """
        try:
            row = {
                "ts": self.clock(), "caller": caller, "model": model,
                "tokens": int(tokens), "est_usd": round(float(est_usd), 6),
            }
            if in_tokens is not None:
                row["in_tokens"] = int(in_tokens)
            if out_tokens is not None:
                row["out_tokens"] = int(out_tokens)
            Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
            with Path(self.log_path).open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass

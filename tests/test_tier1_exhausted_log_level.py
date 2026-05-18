"""
5/18 user complaint：Tier-1 Exhausted 一直 DM 騷擾，但 paid fallback
通常會接住，沒實際影響 user。應該是 WARNING 不是 ERROR。

incident_dispatcher 只接 ERROR/CRITICAL，把這條降成 WARNING 就不會
觸發 DM。Paid Fallback 真的失敗才繼續用 ERROR (那才是真錯)。

Static regression：靜態檢查 gemini_router_llm.py 那行不該是 logger.error。
"""
from __future__ import annotations

import re
from pathlib import Path


def test_tier1_exhausted_uses_warning_not_error():
    src = Path(__file__).parent.parent / "gemini_router_llm.py"
    text = src.read_text(encoding="utf-8")
    # 找 Tier-1 Exhausted log 語句
    matches = re.findall(r'logger\.(\w+)\(f["\'].*?Tier-1 Exhausted', text)
    assert matches, "找不到 Tier-1 Exhausted log 語句（可能改名了）"
    assert all(level == "warning" for level in matches), (
        f"Tier-1 Exhausted 應該用 logger.warning（paid fallback 通常會接住），"
        f"實際 levels: {matches}"
    )


def test_paid_fallback_failure_remains_error():
    """[Paid Fallback] 真的失敗才該是 ERROR（真正喚醒 oncall 的情境）。"""
    src = Path(__file__).parent.parent / "gemini_router_llm.py"
    text = src.read_text(encoding="utf-8")
    matches = re.findall(r'logger\.(\w+)\(f["\'].*?Paid Fallback.*?失敗', text)
    assert matches, "找不到 Paid Fallback 失敗的 log"
    assert "error" in matches, (
        f"Paid Fallback 失敗才是真錯，仍應 logger.error，實際 levels: {matches}"
    )

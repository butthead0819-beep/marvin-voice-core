"""
_meta.review_date 強制推進：daily review 寫入前必須保證 review_date 推進到
目標日期，不依賴 Gemini 回傳 _meta 欄位。

2026-05-24 incident 的次生風險：即使 merge_player 修好，Gemini 偶爾因 token
截斷漏 _meta（_repair_json 補出來的 dict 可能缺 key），原本邏輯是 `if key in
result: final_memory[key] = result[key]` → _meta 沒 → review_date 不推進 → 通知
仍然 success=True → silent 失敗。

修法：寫入前用 _enforce_meta_review_date 強制覆寫 review_date 為 target_date
（backfill 用 args.date，否則用 today），其餘 _meta 欄位保留 Gemini 給的。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path


def _import_module():
    mod_name = "scripts.analyze_daily_log"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    base = Path(__file__).parent.parent
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    return importlib.import_module(mod_name)


# ── 1. Gemini 漏 _meta → review_date 仍推進 ─────────────────────────────────

def test_enforce_review_date_when_meta_missing():
    """Gemini 沒回 _meta → final_memory._meta.review_date 仍被強制設成 target。"""
    mod = _import_module()
    final_memory = {"players": {}}  # 完全沒 _meta
    mod._enforce_meta_review_date(final_memory, "2026-06-01")
    assert final_memory["_meta"]["review_date"] == "2026-06-01"


def test_enforce_review_date_when_meta_has_no_review_date():
    """Gemini 給 _meta 但漏 review_date → 仍被強制設成 target。"""
    mod = _import_module()
    final_memory = {"_meta": {"total_utterances_processed": 50}}
    mod._enforce_meta_review_date(final_memory, "2026-06-01")
    assert final_memory["_meta"]["review_date"] == "2026-06-01"
    assert final_memory["_meta"]["total_utterances_processed"] == 50, "其他欄位保留"


# ── 2. Gemini 回了 _meta + review_date 也對 → 不動 ──────────────────────────

def test_enforce_review_date_matches_target_no_change():
    """Gemini 給的 review_date 已等於 target → 維持原樣。"""
    mod = _import_module()
    final_memory = {"_meta": {"review_date": "2026-06-01", "extra": "keep"}}
    mod._enforce_meta_review_date(final_memory, "2026-06-01")
    assert final_memory["_meta"]["review_date"] == "2026-06-01"
    assert final_memory["_meta"]["extra"] == "keep"


# ── 3. Gemini 回了錯的 review_date → 被覆寫 ──────────────────────────────────

def test_enforce_review_date_overrides_wrong_value():
    """Gemini 回的 review_date 跟 target 不一致（例如幻覺寫舊日期）→ 強制覆寫。"""
    mod = _import_module()
    final_memory = {"_meta": {"review_date": "2026-05-23"}}
    mod._enforce_meta_review_date(final_memory, "2026-06-01")
    assert final_memory["_meta"]["review_date"] == "2026-06-01"


# ── 4. backfill 模式：target 是 slice 日期，不是「今天」 ───────────────────────

def test_enforce_review_date_uses_target_not_today():
    """Backfill 5/24 切片時，review_date 應該是 "2026-05-24"，不是執行當天。"""
    mod = _import_module()
    final_memory = {"_meta": {}}
    mod._enforce_meta_review_date(final_memory, "2026-05-24")
    assert final_memory["_meta"]["review_date"] == "2026-05-24"

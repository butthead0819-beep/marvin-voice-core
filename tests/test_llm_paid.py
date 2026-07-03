"""Tier 3 PaidUsageGuard 測試（Gemini 付費 spending cap）。"""
from __future__ import annotations

from datetime import datetime

import pytest

from llm_paid import PaidUsageGuard, estimate_cost


# 固定一個基準時刻（2026-05-21 12:00 local），方便算 day/month 邊界
_BASE = datetime(2026, 5, 21, 12, 0, 0).timestamp()


def _guard(tmp_path, now=_BASE, daily=5.0, monthly=50.0):
    return PaidUsageGuard(log_path=tmp_path / "paid.jsonl",
                          daily_cap_usd=daily, monthly_cap_usd=monthly,
                          clock=lambda: now)


# ── estimate_cost ────────────────────────────────────────────────────────────

def test_estimate_cost_gemini_pro():
    # 1M in @1.25 + 1M out @10 = 11.25
    assert estimate_cost("gemini-2.5-pro", 1_000_000, 1_000_000) == pytest.approx(11.25)


def test_estimate_cost_unknown_model_uses_default():
    assert estimate_cost("some-unknown", 1_000_000, 0) == pytest.approx(2.0)


def test_estimate_cost_flash_preview_prefix_match():
    # 實際 REVIEW_MODEL 帶版本後綴 → 仍套 flash 價（prefix 比對），非 default
    assert estimate_cost("gemini-2.5-flash-preview-05-20", 1_000_000, 1_000_000) == pytest.approx(2.80)


def test_estimate_cost_flash_lite():
    # Marvin reply paid fallback 用 gemini-3.1-flash-lite-preview；現價約 0.10 / 0.40
    # 不能套 default (2.0/12.0)，會過度高估 ~20×，導致 allow() 過早拒絕。
    assert estimate_cost("gemini-3.1-flash-lite-preview", 1_000_000, 1_000_000) == pytest.approx(0.50)


# ── record + totals ──────────────────────────────────────────────────────────

def test_record_and_spent_today(tmp_path):
    g = _guard(tmp_path)
    g.record(caller="daily_review", model="gemini-2.5-pro", tokens=1000, est_usd=0.30)
    g.record(caller="daily_review", model="gemini-2.5-pro", tokens=2000, est_usd=0.70)
    assert g.spent_today() == pytest.approx(1.00)
    assert g.spent_month() == pytest.approx(1.00)


def test_yesterday_not_counted_today_but_in_month(tmp_path):
    g = _guard(tmp_path)
    # 寫一筆「昨天」的（now - 26h），一筆今天的
    g_yesterday = PaidUsageGuard(log_path=tmp_path / "paid.jsonl", clock=lambda: _BASE - 26 * 3600)
    g_yesterday.record(caller="x", model="gemini-2.5-pro", tokens=1, est_usd=2.0)
    g.record(caller="x", model="gemini-2.5-pro", tokens=1, est_usd=0.5)
    assert g.spent_today() == pytest.approx(0.5)      # 昨天的不算今天
    assert g.spent_month() == pytest.approx(2.5)      # 但都在本月內


# ── allow（cap enforcement）──────────────────────────────────────────────────

def test_allow_under_daily_cap(tmp_path):
    g = _guard(tmp_path, daily=5.0)
    g.record(caller="x", model="gemini-2.5-pro", tokens=1, est_usd=4.0)
    assert g.allow(0.5) is True       # 4.0 + 0.5 <= 5.0


def test_reject_over_daily_cap(tmp_path):
    g = _guard(tmp_path, daily=5.0)
    g.record(caller="x", model="gemini-2.5-pro", tokens=1, est_usd=4.8)
    assert g.allow(0.5) is False      # 4.8 + 0.5 > 5.0


def test_reject_over_monthly_cap(tmp_path):
    g = _guard(tmp_path, daily=100.0, monthly=50.0)   # daily 放寬，測 monthly
    # 本月稍早累積 49.8（用較早 ts 但同月）
    early = PaidUsageGuard(log_path=tmp_path / "paid.jsonl", clock=lambda: _BASE - 5 * 86400)
    early.record(caller="x", model="gemini-2.5-pro", tokens=1, est_usd=49.8)
    assert g.allow(0.5) is False      # 49.8 + 0.5 > 50.0


def test_empty_log_allows(tmp_path):
    g = _guard(tmp_path)
    assert g.spent_today() == 0.0
    assert g.allow(4.9) is True


def test_corrupt_line_skipped(tmp_path):
    p = tmp_path / "paid.jsonl"
    p.write_text('{"ts": %f, "est_usd": 1.0}\nGARBAGE NOT JSON\n{"ts": %f, "est_usd": 2.0}\n' % (_BASE, _BASE),
                 encoding="utf-8")
    g = _guard(tmp_path)
    assert g.spent_today() == pytest.approx(3.0)   # 壞行跳過，好行照算


def test_default_caps_aligned_with_10usd_spending_cap():
    """2026-07-03 使用者訂：GCP spending cap 只有 $10 → 預設閘收緊。

    daily 0.5（防 runaway 輸出型事故一天燒掉月預算）、monthly 4.0
    （最壞情況 $10 也撐 2.5 個月；典型月 ~$2 撐 4-5 個月）。
    """
    from llm_paid import PaidUsageGuard
    g = PaidUsageGuard()
    assert g.daily_cap_usd == 0.5
    assert g.monthly_cap_usd == 4.0


def test_record_stores_in_out_token_split(tmp_path):
    """2026-07-03：in/out 分開存——output 單價是 input 8 倍，混在一起無法診斷
    「誰在生成大量輸出」（本次帳務調查的痛點正是 output token SKU）。"""
    import json
    from llm_paid import PaidUsageGuard
    g = PaidUsageGuard(log_path=tmp_path / "u.jsonl")
    g.record(caller="test", model="gemini-2.5-flash", tokens=300, est_usd=0.01,
             in_tokens=200, out_tokens=100)
    row = json.loads((tmp_path / "u.jsonl").read_text().strip())
    assert row["in_tokens"] == 200
    assert row["out_tokens"] == 100


def test_record_without_split_still_works(tmp_path):
    """舊呼叫介面不帶 split → 不炸、不寫 split 欄（向後相容）。"""
    import json
    from llm_paid import PaidUsageGuard
    g = PaidUsageGuard(log_path=tmp_path / "u.jsonl")
    g.record(caller="test", model="m", tokens=10, est_usd=0.0)
    row = json.loads((tmp_path / "u.jsonl").read_text().strip())
    assert "in_tokens" not in row

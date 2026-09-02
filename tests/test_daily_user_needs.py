"""daily_user_needs.py — 日界切要用本地 (UTC+8) 時區、已 resolved 的 intent_type 要標示
（review 意見：PR #76 commit b63275b）。

1. `--date`/預設「昨天」原本用 UTC 日界切，在 production Asia/Taipei 下 --date 2026-08-29
   會漏當地凌晨、混入隔天早上，daily 報表貼錯天。改用 UTC+8。
2. agent_gaps_resolved.json 收錄的 intent_type（已有 agent，只是冷門說法漏接）原本
   跟真正缺 agent 的需求混在一起列示，容易讓 ritual 誤產出「重做 agent」的建議；
   改為載入 resolved 清單並標示，且排序上排到未 resolved 之後（同 analyze_agent_gaps 慣例：
   仍可見、不默默消失，只是不再算新缺口）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from scripts.daily_user_needs import _day_bounds, _TPE_TZ, section_needs


def test_day_bounds_date_uses_taipei_local_midnight_not_utc():
    lo, hi, label = _day_bounds("2026-08-29", 1)
    lo_dt = datetime.fromtimestamp(lo, tz=timezone.utc)
    hi_dt = datetime.fromtimestamp(hi, tz=timezone.utc)

    # 2026-08-29 00:00 UTC+8 == 2026-08-28 16:00 UTC
    assert lo_dt == datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc)
    assert hi_dt == datetime(2026, 8, 29, 16, 0, tzinfo=timezone.utc)
    assert label == "2026-08-29"


def test_day_bounds_default_yesterday_anchors_to_taipei_today():
    lo, hi, _ = _day_bounds(None, 1)
    now_tpe = datetime.now(_TPE_TZ)
    expected_today = now_tpe.replace(hour=0, minute=0, second=0, microsecond=0)
    assert hi == expected_today.timestamp()
    assert lo == (expected_today - timedelta(days=1)).timestamp()


def test_resolved_intent_type_labeled_not_hidden(tmp_path, monkeypatch):
    gaps = tmp_path / "agent_gaps.jsonl"
    resolved_json = tmp_path / "agent_gaps_resolved.json"
    ts = 1000.0
    gaps.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"intent_type": "time_query", "speaker": "a", "raw_query": "現在幾點", "ts": ts},
                {"intent_type": "buy_milk", "speaker": "b", "raw_query": "幫我買牛奶", "ts": ts},
            ]
        ),
        encoding="utf-8",
    )
    resolved_json.write_text(json.dumps({"time_query": {"agent": "time_query"}}), encoding="utf-8")

    monkeypatch.setattr("scripts.daily_user_needs.GAPS", gaps)
    monkeypatch.setattr("scripts.daily_user_needs.RESOLVED", resolved_json)

    out = section_needs(0, 2000.0)

    assert "既有 agent coverage" in out
    time_query_line = next(l for l in out.splitlines() if "time_query" in l)
    buy_milk_line = next(l for l in out.splitlines() if "buy_milk" in l)
    assert "既有 agent coverage" in time_query_line
    assert "既有 agent coverage" not in buy_milk_line
    # 未 resolved 的排在 resolved 前面
    assert out.index("`buy_milk`") < out.index("`time_query`")


def test_no_resolved_file_behaves_as_before(tmp_path, monkeypatch):
    gaps = tmp_path / "agent_gaps.jsonl"
    gaps.write_text(
        json.dumps({"intent_type": "buy_milk", "speaker": "b", "raw_query": "牛奶", "ts": 1.0}),
        encoding="utf-8",
    )
    monkeypatch.setattr("scripts.daily_user_needs.GAPS", gaps)
    monkeypatch.setattr("scripts.daily_user_needs.RESOLVED", tmp_path / "does_not_exist.json")

    out = section_needs(0, 2000.0)
    assert "既有 agent coverage（resolved" not in out


def test_section_2c_surfaces_abandoned_rescue(tmp_path, monkeypatch):
    """rescue abandoned（synthesize 回 None）要進 daily 2c，依 abandon_reason 分組。"""
    from scripts.daily_user_needs import section_complaints

    rescue = tmp_path / "rescue_outcomes.jsonl"
    rescue.write_text(
        "\n".join(json.dumps(r) for r in [
            {"gap_class": "abandoned", "abandon_reason": "just_chatting",
             "original_query": "欸馬文那個那個", "speaker": "showay", "ts": 1000.0},
            {"gap_class": "abandoned", "abandon_reason": "gemini_error:400 blah",
             "original_query": "放那個歌", "speaker": "狗與露", "ts": 1001.0},
            {"gap_class": "convergent", "pragmatic_signal": "neutral",
             "original_query": "下一首", "speaker": "showay", "ts": 1002.0},
        ]),
        encoding="utf-8",
    )
    monkeypatch.setattr("scripts.daily_user_needs.RESCUE", rescue)
    monkeypatch.setattr("scripts.daily_user_needs.DB", tmp_path / "no.db")

    out = section_complaints(0, 2000.0)

    assert "2c. rescue abandoned" in out
    assert "— 2 筆" in out.split("2c. rescue abandoned")[1][:20]
    assert "`just_chatting`" in out
    assert "`gemini_error`" in out          # 冒號後的細節被切掉、只留分類
    assert "欸馬文那個那個" in out

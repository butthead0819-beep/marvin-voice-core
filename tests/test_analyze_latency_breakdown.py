"""
分析 voice pipeline 延遲組成：grep STAGE_TIMING（前半）+ TTS_TIMING（首音）+
llm_routing.jsonl（回應 LLM），重建「使用者停話→首音」完整鏈，定位 baseline
2-3s 卡哪段。

純函式（line parser / percentile / stage 拆解）在此測；IO main 在腳本內。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path


def _mod():
    name = "scripts.analyze_latency_breakdown"
    if name in sys.modules:
        del sys.modules[name]
    base = Path(__file__).parent.parent
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    return importlib.import_module(name)


# ── STAGE_TIMING parser ──────────────────────────────────────────────────────

_STAGE_LINE = (
    "2026-06-01 22:01:03 [INFO] [STAGE_TIMING] speaker=狗與露 sttstart=12ms "
    "sttdone=487ms cleanerdone=1203ms intentdispatched=1208ms total=1208ms "
    "text='播放周杰倫的稻香' route=main_bus"
)


def test_parse_stage_timing_extracts_all_stages():
    m = _mod()
    r = m.parse_stage_timing(_STAGE_LINE)
    assert r is not None
    assert r["sttstart"] == 12
    assert r["sttdone"] == 487
    assert r["cleanerdone"] == 1203
    assert r["intentdispatched"] == 1208
    assert r["total"] == 1208


def test_parse_stage_timing_non_matching_returns_none():
    m = _mod()
    assert m.parse_stage_timing("just a random log line rtcp_packet") is None


def test_parse_stage_timing_partial_stages_ok():
    """有些 line 只到 cleanerdone（game/nowake route）→ 缺的欄位不報錯。"""
    m = _mod()
    line = "[STAGE_TIMING] speaker=X sttstart=10ms sttdone=300ms total=300ms text='hi'"
    r = m.parse_stage_timing(line)
    assert r is not None
    assert r["sttstart"] == 10
    assert r["sttdone"] == 300
    assert "cleanerdone" not in r


# ── stage 拆解：累積 ms → 各段 duration ──────────────────────────────────────


def test_stage_durations_derives_per_stage():
    m = _mod()
    r = {"sttstart": 12, "sttdone": 487, "cleanerdone": 1203, "intentdispatched": 1208}
    d = m.stage_durations(r)
    assert d["stt"] == 487 - 12          # STT 轉錄
    assert d["cleaner"] == 1203 - 487    # cleaner LLM
    assert d["intent"] == 1208 - 1203    # intent dispatch
    assert d["pre_stt"] == 12            # endpoint→stt 開始


def test_stage_durations_skips_missing():
    m = _mod()
    r = {"sttstart": 10, "sttdone": 300}
    d = m.stage_durations(r)
    assert d["stt"] == 290
    assert "cleaner" not in d


# ── TTS_TIMING parser ────────────────────────────────────────────────────────


def test_parse_tts_timing():
    m = _mod()
    line = "2026-06-01 22:01:05 [INFO] [TTS_TIMING] first_audio=842ms chars=18 macos=False text='好啦'"
    r = m.parse_tts_timing(line)
    assert r is not None
    assert r["first_audio"] == 842
    assert r["chars"] == 18


def test_parse_tts_timing_non_matching():
    m = _mod()
    assert m.parse_tts_timing("nope") is None


# ── percentile ───────────────────────────────────────────────────────────────


def test_percentile():
    m = _mod()
    xs = list(range(1, 101))  # 1..100
    assert m.percentile(xs, 0.5) == 50 or m.percentile(xs, 0.5) == 51
    assert m.percentile(xs, 0.9) >= 90


def test_percentile_empty_returns_none():
    m = _mod()
    assert m.percentile([], 0.5) is None


# ── llm_routing 今日過濾（ts-based）──────────────────────────────────────────


def test_filter_recent_by_ts():
    m = _mod()
    rows = [
        {"ts": 1000.0, "latency_ms": 100, "success": True},
        {"ts": 5000.0, "latency_ms": 200, "success": True},
        {"ts": 9000.0, "latency_ms": 300, "success": False},
    ]
    recent = m.filter_recent(rows, since_ts=4000.0)
    assert len(recent) == 2
    assert all(r["ts"] >= 4000.0 for r in recent)

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


def test_stage_durations_splits_cleaner_into_three():
    """有 dequeued + questiondone 中間打點時，舊的單一 cleaner 段拆成三段：
    queue_wait（排隊）/ question_wait（等使用者講完問句）/ cleaner_pure（真清洗）。
    且不再產生會誤導的 legacy cleaner 段。"""
    m = _mod()
    r = {
        "sttstart": 12, "sttdone": 487,
        "dequeued": 9000, "questiondone": 11000, "cleanerdone": 11300,
        "intentdispatched": 11305,
    }
    d = m.stage_durations(r)
    assert d["queue_wait"] == 9000 - 487       # 排隊等 worker
    assert d["question_wait"] == 11000 - 9000  # evt.wait 等問句
    assert d["cleaner_pure"] == 11300 - 11000  # 真 cleaner LLM
    assert d["intent"] == 11305 - 11300
    assert "cleaner" not in d, "有中間打點時不該再算 legacy 混合段"


def test_stage_durations_legacy_keeps_single_cleaner():
    """舊 log 行（無 dequeued/questiondone）→ 維持 legacy cleaner 段，向後相容。"""
    m = _mod()
    r = {"sttstart": 12, "sttdone": 487, "cleanerdone": 1203, "intentdispatched": 1208}
    d = m.stage_durations(r)
    assert d["cleaner"] == 1203 - 487
    assert "queue_wait" not in d
    assert "cleaner_pure" not in d


def test_parse_stage_timing_extracts_new_keys():
    m = _mod()
    line = (
        "[STAGE_TIMING] speaker=X sttstart=10ms sttdone=300ms dequeued=9000ms "
        "questiondone=11000ms cleanerdone=11300ms intentdispatched=11305ms total=11305ms text='hi'"
    )
    r = m.parse_stage_timing(line)
    assert r is not None
    assert r["dequeued"] == 9000
    assert r["questiondone"] == 11000


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


# ── 輪轉備份讀取：bot_stdout.log 5MB 輪轉成 .1/.2/.3，timing 散在多檔 ──────────


def test_read_log_lines_includes_rotated_backups(tmp_path):
    """主檔 + .1/.2/.3 都要讀，否則輪轉出去的 STAGE_TIMING/TTS_TIMING 漏掉。"""
    m = _mod()
    (tmp_path / "bot_stdout.log").write_text(
        "noise\n[STAGE_TIMING] speaker=A sttstart=1ms sttdone=2ms total=2ms text='x'\n",
        encoding="utf-8",
    )
    (tmp_path / "bot_stdout.log.1").write_text(
        "[TTS_TIMING] first_audio=500ms chars=3 text='a'\nrtcp noise\n", encoding="utf-8"
    )
    (tmp_path / "bot_stdout.log.2").write_text(
        "[STAGE_TIMING] speaker=B sttstart=5ms sttdone=9ms total=9ms text='y'\n",
        encoding="utf-8",
    )

    lines = m._read_log_lines(tmp_path / "bot_stdout.log")
    joined = "\n".join(lines)
    assert "speaker=A" in joined       # 主檔
    assert "first_audio=500ms" in joined  # .1
    assert "speaker=B" in joined       # .2
    # 只回含 timing tag 的行（noise 濾掉）
    assert all("STAGE_TIMING" in l or "TTS_TIMING" in l for l in lines)


def test_read_log_lines_missing_file_returns_empty(tmp_path):
    m = _mod()
    assert m._read_log_lines(tmp_path / "nope.log") == []


def test_line_timestamp_parses_prefix():
    m = _mod()
    ts = m.line_timestamp("2026-06-02 07:23:52,743 [INFO] [STAGE_TIMING] speaker=A total=2ms")
    assert ts is not None
    from datetime import datetime
    assert datetime.fromtimestamp(ts).year == 2026
    assert m.line_timestamp("no timestamp here") is None


def test_read_log_lines_window_filters_old(tmp_path):
    """since_ts 給定時，舊故障期的 timing 行被濾掉（與 llm_routing 24h 窗一致）。"""
    m = _mod()
    from datetime import datetime
    old = "2026-05-26 03:00:00,000 [INFO] [STAGE_TIMING] speaker=OLD total=99ms text='x'"
    new = "2026-06-02 06:00:00,000 [INFO] [STAGE_TIMING] speaker=NEW total=2ms text='y'"
    (tmp_path / "bot_stdout.log").write_text(old + "\n" + new + "\n", encoding="utf-8")
    since = datetime(2026, 6, 1).timestamp()
    lines = m._read_log_lines(tmp_path / "bot_stdout.log", since_ts=since)
    joined = "\n".join(lines)
    assert "speaker=NEW" in joined
    assert "speaker=OLD" not in joined

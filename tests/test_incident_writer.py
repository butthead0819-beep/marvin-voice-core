"""incident_writer: forensic log extraction → structured markdown incident.

設計理念（與 user 對齊）：
  - openclaw / LLM 在這層 **沒角色**——事件時間軸是確定性 Python 工作
  - 報告只有事實（log 時間軸 + 觸發 context），**沒有 hypothesis**
  - 讓 Claude Code 從乾淨的事實開始調查，不被錯誤的猜測誤導
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest


@pytest.fixture
def make_record():
    def _make(*, name="gemini_router_llm", level=logging.ERROR,
              msg="❌ [Tier-1 Exhausted] 雲端重試 3 次後依然失敗",
              ts=None, exc_info=None):
        rec = logging.LogRecord(
            name=name, level=level, pathname="x.py", lineno=1,
            msg=msg, args=(), exc_info=exc_info,
        )
        if ts is not None:
            rec.created = ts
        return rec
    return _make


@pytest.fixture
def sample_log(tmp_path):
    """A realistic log file mixing noise + signal."""
    log = tmp_path / "bot_main.log"
    base_ts = datetime(2026, 5, 18, 7, 44, 0)
    lines = []
    for i, (offset_s, level, name, msg) in enumerate([
        (-90, "DEBUG", "discord.ext.voice_recv.router", "Dispatching voice_client event rtcp_packet"),
        (-58, "INFO",  "STTHistory", "[狗與露] (Debounced) 馬文今天天氣怎麼樣"),
        (-45, "INFO",  "cogs.voice_controller", "🔍 [Cloud Oracle] 偵測到即時資訊需求"),
        (-30, "WARNING", "gemini_router_llm", "Tier-1 attempt 1 failed: 500 INTERNAL"),
        (-20, "WARNING", "gemini_router_llm", "Tier-1 attempt 2 failed: 500 INTERNAL"),
        (-10, "ERROR", "discord.ext.voice_recv.reader", "Error unpacking packet"),  # noise
        (-5,  "WARNING", "gemini_router_llm", "Tier-1 attempt 3 failed: 500 INTERNAL"),
        (0,   "ERROR", "gemini_router_llm", "❌ [Tier-1 Exhausted] 雲端重試 3 次後依然失敗: 500 INTERNAL"),
        (5,   "INFO",  "cogs.voice_controller", "📊 [Dynamic Social] VAD Delay: 0.8s"),
    ]):
        t = base_ts + timedelta(seconds=offset_s)
        lines.append(f"{t.strftime('%Y-%m-%d %H:%M:%S')},000 [{level}] {name}: {msg}")
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log, base_ts


# ── Filename + dir creation ──────────────────────────────────────────────────

def test_creates_output_directory_if_missing(tmp_path, sample_log, make_record):
    from incident_writer import write_incident

    out = tmp_path / "incidents"
    assert not out.exists()
    log, base_ts = sample_log
    rec = make_record(ts=base_ts.timestamp())
    write_incident(record=rec, log_path=log, output_dir=out)
    assert out.is_dir()


def test_filename_format_has_ts_and_slug(tmp_path, sample_log, make_record):
    from incident_writer import write_incident

    log, base_ts = sample_log
    rec = make_record(ts=base_ts.timestamp())
    p = write_incident(record=rec, log_path=log, output_dir=tmp_path)
    # 期待：2026-05-18-074400-gemini-router-llm-tier1-exhausted.md（或類似）
    assert p.name.startswith("2026-05-18-074400-")
    assert p.suffix == ".md"
    assert "tier" in p.name.lower() or "exhausted" in p.name.lower()


def test_returns_path_of_written_file(tmp_path, sample_log, make_record):
    from incident_writer import write_incident

    log, base_ts = sample_log
    rec = make_record(ts=base_ts.timestamp())
    p = write_incident(record=rec, log_path=log, output_dir=tmp_path)
    assert p.exists()
    assert p.read_text(encoding="utf-8").startswith("---\n")


# ── Frontmatter ──────────────────────────────────────────────────────────────

def test_frontmatter_has_required_fields(tmp_path, sample_log, make_record):
    from incident_writer import write_incident

    log, base_ts = sample_log
    rec = make_record(ts=base_ts.timestamp())
    p = write_incident(record=rec, log_path=log, output_dir=tmp_path, recurrence_24h=7)
    fm = p.read_text(encoding="utf-8").split("---")[1]
    assert "ts: 2026-05-18T07:44:00" in fm
    assert "logger: gemini_router_llm" in fm
    assert "level: ERROR" in fm
    assert "severity: med" in fm  # 普通 ERROR，沒 exc_info
    assert "recurrence_24h: 7" in fm
    assert "status: open" in fm


def test_severity_high_for_critical_level(tmp_path, sample_log, make_record):
    from incident_writer import write_incident

    log, base_ts = sample_log
    rec = make_record(level=logging.CRITICAL,
                      msg="💀 [Sentinel] 主腦完全失聯", ts=base_ts.timestamp())
    p = write_incident(record=rec, log_path=log, output_dir=tmp_path)
    assert "severity: high" in p.read_text(encoding="utf-8")


def test_severity_high_for_unhandled_traceback(tmp_path, sample_log, make_record):
    from incident_writer import write_incident

    log, base_ts = sample_log
    try:
        raise ValueError("boom")
    except ValueError:
        exc = sys.exc_info()
    rec = make_record(exc_info=exc, ts=base_ts.timestamp())
    p = write_incident(record=rec, log_path=log, output_dir=tmp_path)
    assert "severity: high" in p.read_text(encoding="utf-8")


# ── Timeline extraction ──────────────────────────────────────────────────────

def test_timeline_includes_window_before_trigger(tmp_path, sample_log, make_record):
    from incident_writer import write_incident

    log, base_ts = sample_log
    rec = make_record(ts=base_ts.timestamp())
    p = write_incident(record=rec, log_path=log, output_dir=tmp_path,
                       context_window_seconds=60)
    body = p.read_text(encoding="utf-8")
    # -58s 應該被抓到（剛好在 60s 窗內）
    assert "馬文今天天氣怎麼樣" in body
    # -90s 不該被抓到（超出窗）
    assert "rtcp_packet" not in body
    # 三個 Tier-1 attempt warnings 都該抓到
    assert "Tier-1 attempt 1" in body
    assert "Tier-1 attempt 2" in body
    assert "Tier-1 attempt 3" in body


def test_timeline_excludes_noise_lines(tmp_path, sample_log, make_record):
    from incident_writer import write_incident

    log, base_ts = sample_log
    rec = make_record(ts=base_ts.timestamp())
    p = write_incident(record=rec, log_path=log, output_dir=tmp_path)
    body = p.read_text(encoding="utf-8")
    # voice_recv.reader / discord.player / yt-dlp deadlock / Restart / TPM Guard 都該過濾
    assert "Error unpacking packet" not in body
    assert "discord.ext.voice_recv" not in body


def test_timeline_handles_missing_log_file(tmp_path, make_record):
    from incident_writer import write_incident

    rec = make_record()
    p = write_incident(record=rec,
                       log_path=tmp_path / "nope.log",
                       output_dir=tmp_path)
    body = p.read_text(encoding="utf-8")
    # 找不到 log 不該 raise；報告本身仍要寫
    assert "(log file not found" in body or "(no log context available" in body


# ── 涉及 logger 區塊 ──────────────────────────────────────────────────────────

def test_involved_loggers_section_dedupes_and_sorts(tmp_path, sample_log, make_record):
    from incident_writer import write_incident

    log, base_ts = sample_log
    rec = make_record(ts=base_ts.timestamp())
    p = write_incident(record=rec, log_path=log, output_dir=tmp_path)
    body = p.read_text(encoding="utf-8")
    assert "## 涉及 logger" in body
    # 至少要有 gemini_router_llm + cogs.voice_controller + STTHistory
    assert "gemini_router_llm" in body
    assert "cogs.voice_controller" in body
    # 噪音 logger 不該出現
    assert "discord.ext.voice_recv" not in body


# ── Traceback 區塊 ───────────────────────────────────────────────────────────

def test_traceback_section_present_when_exc_info(tmp_path, sample_log, make_record):
    from incident_writer import write_incident

    log, base_ts = sample_log
    try:
        raise KeyError("missing_key_42")
    except KeyError:
        exc = sys.exc_info()
    rec = make_record(exc_info=exc, ts=base_ts.timestamp())
    p = write_incident(record=rec, log_path=log, output_dir=tmp_path)
    body = p.read_text(encoding="utf-8")
    assert "## Traceback" in body
    assert "KeyError" in body
    assert "missing_key_42" in body


def test_traceback_section_absent_when_no_exc_info(tmp_path, sample_log, make_record):
    from incident_writer import write_incident

    log, base_ts = sample_log
    rec = make_record(ts=base_ts.timestamp())
    p = write_incident(record=rec, log_path=log, output_dir=tmp_path)
    body = p.read_text(encoding="utf-8")
    assert "## Traceback" not in body


# ── Action items ─────────────────────────────────────────────────────────────

def test_action_items_section_for_claude_code(tmp_path, sample_log, make_record):
    from incident_writer import write_incident

    log, base_ts = sample_log
    rec = make_record(ts=base_ts.timestamp())
    p = write_incident(record=rec, log_path=log, output_dir=tmp_path)
    body = p.read_text(encoding="utf-8")
    assert "## Claude Code action items" in body
    assert "- [ ]" in body  # 至少有一個未完成 checkbox

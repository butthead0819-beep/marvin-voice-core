"""Forensic log → structured markdown incident.

讓 Claude Code 從乾淨的事實開始調查；報告只有時間軸 + 觸發 context，沒有任何 hypothesis。
"""
from __future__ import annotations

import logging
import re
import traceback as _tb
from datetime import datetime, timedelta
from pathlib import Path

# 共用 ErrorDispatcher 的噪音規則（避免時間軸區塊重新出現已被擋掉的訊號）
from error_dispatcher import (
    _NOISE_LOGGER_PREFIXES,
    _NOISE_MESSAGE_PATTERNS,
)


# ── log line parsing ─────────────────────────────────────────────────────────

# Matches the project's RotatingFileHandler format:
#   "2026-05-18 07:44:00,000 [ERROR] gemini_router_llm: message..."
_LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
    r",(?P<ms>\d{3})\s+"
    r"\[(?P<level>[A-Z]+)\]\s+"
    r"(?P<logger>[\w\.\-]+):\s+"
    r"(?P<msg>.*)$"
)

# 解析時間軸用，DEBUG 一律剔除（log 噪音量級太大）
_NOISE_LEVELS = {"DEBUG"}


def _is_noise_line(logger_name: str, msg: str) -> bool:
    for prefix in _NOISE_LOGGER_PREFIXES:
        if logger_name.startswith(prefix):
            return True
    for pat in _NOISE_MESSAGE_PATTERNS:
        if pat.search(msg):
            return True
    return False


def _slugify(text: str, max_len: int = 50) -> str:
    """Lossy filesystem-safe slug. 抽中英文+數字，dash 分段，截斷。"""
    # 抽英數 + 連字
    tokens = re.findall(r"[A-Za-z0-9]+", text.lower())
    slug = "-".join(tokens)[:max_len].strip("-")
    return slug or "incident"


def _classify_severity(record: logging.LogRecord) -> str:
    if record.levelno >= logging.CRITICAL:
        return "high"
    if record.exc_info:
        return "high"
    return "med"


# ── main API ─────────────────────────────────────────────────────────────────

def write_incident(
    *,
    record: logging.LogRecord,
    log_path: str | Path,
    output_dir: str | Path = ".claude_todo/incidents",
    context_window_seconds: int = 60,
    recurrence_24h: int = 1,
) -> Path:
    """Extract event timeline from log_path around the trigger and write a markdown incident.

    Returns the absolute path of the written file.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    trigger_ts = datetime.fromtimestamp(record.created)
    severity = _classify_severity(record)
    msg = record.getMessage()

    # Filename: 2026-05-18-074400-<logger>-<msg-slug>.md
    slug_parts = [_slugify(record.name, max_len=20), _slugify(msg, max_len=30)]
    slug = "-".join(p for p in slug_parts if p)
    filename = f"{trigger_ts.strftime('%Y-%m-%d-%H%M%S')}-{slug}.md"
    path = output_dir / filename

    timeline_lines, involved_loggers = _extract_timeline(
        log_path=Path(log_path),
        trigger_ts=trigger_ts,
        window_seconds=context_window_seconds,
    )

    tb_section = ""
    if record.exc_info:
        tb_text = "".join(_tb.format_exception(*record.exc_info))[:3000]
        tb_section = f"\n## Traceback\n```\n{tb_text}\n```\n"

    timeline_block = "\n".join(timeline_lines) if timeline_lines else "(no log context available)"
    loggers_block = "\n".join(f"- {n}" for n in sorted(involved_loggers)) or "- (none extracted)"

    body = f"""---
ts: {trigger_ts.strftime('%Y-%m-%dT%H:%M:%S')}
logger: {record.name}
level: {record.levelname}
signature: {record.name}:{record.levelname}:{msg[:80]}
severity: {severity}
recurrence_24h: {recurrence_24h}
status: open
---

## 錯誤
```
{msg}
```

## 觸發前 {context_window_seconds} 秒事件時間軸（已過濾噪音）
```
{timeline_block}
```

## 涉及 logger
{loggers_block}
{tb_section}
## Claude Code action items
- [ ] 確認 root cause（讀涉及 logger 對應 source；別信任 message 表面文字）
- [ ] 寫 failing test 重現問題
- [ ] 修復並 commit
- [ ] 把 frontmatter 的 `status: open` 改成 `status: resolved` 並留 commit hash
"""
    path.write_text(body, encoding="utf-8")
    return path


# ── timeline extraction ──────────────────────────────────────────────────────

def _extract_timeline(
    *, log_path: Path, trigger_ts: datetime, window_seconds: int,
) -> tuple[list[str], set[str]]:
    """Read log_path; return (filtered lines within window, set of involved loggers)."""
    if not log_path.exists():
        return [f"(log file not found: {log_path})"], set()

    start_ts = trigger_ts - timedelta(seconds=window_seconds)
    end_ts = trigger_ts + timedelta(seconds=5)  # 抓觸發後 5 秒當餘震
    keep: list[str] = []
    loggers: set[str] = set()

    try:
        # 大檔，避免一次載入；逐行掃。bot_main.log 最大 10MB 可接受。
        with log_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _LOG_LINE_RE.match(line.rstrip("\n"))
                if not m:
                    continue
                level = m.group("level")
                if level in _NOISE_LEVELS:
                    continue
                logger_name = m.group("logger")
                msg = m.group("msg")
                if _is_noise_line(logger_name, msg):
                    continue
                try:
                    line_ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                if line_ts < start_ts:
                    continue
                if line_ts > end_ts:
                    # log 是時序排序，可提前 break
                    break
                keep.append(line.rstrip("\n"))
                loggers.add(logger_name)
    except OSError as e:
        return [f"(log file read failed: {e})"], set()

    return keep, loggers

"""測試 scripts/prune_retention.py 的資料保留與清理。"""
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
import pytest
import memory_sandbox


def _create_schema(db_path: Path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE speaker_topic_graph (
            transcript_id INTEGER PRIMARY KEY AUTOINCREMENT,
            speaker TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            embedding BLOB,
            emotion_text TEXT,
            emotion_prosody TEXT,
            last_bridged_at REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE session_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            window_start REAL NOT NULL,
            window_end REAL NOT NULL,
            summary_text TEXT NOT NULL,
            speakers TEXT NOT NULL DEFAULT '[]',
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            direction TEXT NOT NULL,
            assignee TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            due_date REAL,
            source_quote TEXT NOT NULL DEFAULT '',
            source_window_start REAL NOT NULL,
            source_window_end REAL NOT NULL,
            created_at REAL NOT NULL,
            speaker TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.commit()
    conn.close()


def test_prune_retention_dry_run_and_apply(tmp_path):
    db_file = tmp_path / "test_marvin.db"
    _create_schema(db_file)

    now = 1790800000.0  # 固定時間
    cutoff_30d = now - 30 * 86400
    cutoff_90d = now - 90 * 86400

    conn = sqlite3.connect(db_file)
    # 1. speaker_topic_graph: 1 筆過期 (35天前), 1 筆未過期 (10天前)
    conn.execute(
        "INSERT INTO speaker_topic_graph (speaker, channel_id, text, created_at) VALUES ('Alice', 1, 'old topic', ?)",
        (now - 35 * 86400,),
    )
    conn.execute(
        "INSERT INTO speaker_topic_graph (speaker, channel_id, text, created_at) VALUES ('Bob', 1, 'new topic', ?)",
        (now - 10 * 86400,),
    )

    # 2. session_summaries: 1 筆過期 (40天前), 1 筆未過期 (5天前)
    conn.execute(
        "INSERT INTO session_summaries (guild_id, window_start, window_end, summary_text, created_at) VALUES (1, 0, 10, 'old sum', ?)",
        (now - 40 * 86400,),
    )
    conn.execute(
        "INSERT INTO session_summaries (guild_id, window_start, window_end, summary_text, created_at) VALUES (1, 0, 10, 'new sum', ?)",
        (now - 5 * 86400,),
    )

    # 3. tasks:
    # - done 過期 (35天前) -> 應刪除
    # - cancelled 過期 (35天前) -> 應刪除
    # - pending 過期 (100天前) -> 應保留！
    # - done 未過期 (5天前) -> 應保留
    conn.execute(
        "INSERT INTO tasks (guild_id, text, direction, assignee, status, source_window_start, source_window_end, created_at) VALUES (1, 't1', 'in', 'u', 'done', 0, 0, ?)",
        (now - 35 * 86400,),
    )
    conn.execute(
        "INSERT INTO tasks (guild_id, text, direction, assignee, status, source_window_start, source_window_end, created_at) VALUES (1, 't2', 'in', 'u', 'cancelled', 0, 0, ?)",
        (now - 35 * 86400,),
    )
    conn.execute(
        "INSERT INTO tasks (guild_id, text, direction, assignee, status, source_window_start, source_window_end, created_at) VALUES (1, 't3', 'in', 'u', 'pending', 0, 0, ?)",
        (now - 100 * 86400,),
    )
    conn.execute(
        "INSERT INTO tasks (guild_id, text, direction, assignee, status, source_window_start, source_window_end, created_at) VALUES (1, 't4', 'in', 'u', 'done', 0, 0, ?)",
        (now - 5 * 86400,),
    )
    conn.commit()
    conn.close()

    # 4. daily records
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    # 2026-10-01 往回算：
    # 15天前過期 (stt_2026-09-10.log, 2026-09-10.log) -> 應刪除
    # 5天前未過期 (2026-09-26.log) -> 保留
    # review_cron.log 不合格式 -> 永遠保留
    (daily_dir / "stt_2026-09-10.log").write_text("old stt")
    (daily_dir / "2026-09-10.log").write_text("old log")
    (daily_dir / "topic_stats_2026-09-10.json").write_text("{}")
    (daily_dir / "2026-09-26.log").write_text("new log")
    (daily_dir / "review_cron.log").write_text("cron log")

    # 5. presence log: 1 筆過期 (100天前), 1 筆未過期 (10天前)
    presence_file = tmp_path / "voice_presence.jsonl"
    presence_records = [
        {"ts": now - 100 * 86400, "user_id": "1", "event": "join"},
        {"ts": now - 10 * 86400, "user_id": "1", "event": "leave"},
    ]
    presence_file.write_text("\n".join(json.dumps(r) for r in presence_records) + "\n")

    script_path = Path(__file__).resolve().parent.parent / "scripts" / "prune_retention.py"

    # --- 1. Dry run ---
    res_dry = subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--db", str(db_file),
            "--daily-dir", str(daily_dir),
            "--presence", str(presence_file),
            "--now", str(now),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    summary_dry = json.loads(res_dry.stdout.strip())
    assert summary_dry["dry_run"] is True
    assert summary_dry["speaker_topic_graph"]["matched"] == 1
    assert summary_dry["session_summaries"]["matched"] == 1
    assert summary_dry["tasks"]["matched"] == 2
    assert summary_dry["daily_files"]["matched"] == 3
    assert summary_dry["voice_presence"]["matched"] == 1

    # 驗證 dry-run 下檔案與 DB 都沒變動
    conn = sqlite3.connect(db_file)
    assert conn.execute("SELECT count(*) FROM speaker_topic_graph").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM session_summaries").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 4
    conn.close()
    assert (daily_dir / "stt_2026-09-10.log").exists()
    assert len(presence_file.read_text().strip().splitlines()) == 2
    assert len(list(tmp_path.glob("voice_presence.jsonl.bak_*"))) == 0

    # --- 2. Apply ---
    res_apply = subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--db", str(db_file),
            "--daily-dir", str(daily_dir),
            "--presence", str(presence_file),
            "--now", str(now),
            "--apply",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    summary_apply = json.loads(res_apply.stdout.strip())
    assert summary_apply["dry_run"] is False
    assert summary_apply["speaker_topic_graph"]["deleted"] == 1
    assert summary_apply["session_summaries"]["deleted"] == 1
    assert summary_apply["tasks"]["deleted"] == 2
    assert summary_apply["daily_files"]["deleted"] == 3
    assert summary_apply["voice_presence"]["deleted"] == 1

    # 驗證 apply 下的 DB 狀態
    conn = sqlite3.connect(db_file)
    # speaker_topic_graph 只剩未過期的 Bob
    rows = conn.execute("SELECT speaker FROM speaker_topic_graph").fetchall()
    assert len(rows) == 1 and rows[0][0] == "Bob"
    # session_summaries 只剩未過期的 new sum
    rows = conn.execute("SELECT summary_text FROM session_summaries").fetchall()
    assert len(rows) == 1 and rows[0][0] == "new sum"
    # tasks: done (old), cancelled (old) 被刪；pending (old) 與 done (new) 保留！
    rows = conn.execute("SELECT text FROM tasks ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["t3", "t4"]
    conn.close()

    # 驗證 daily_dir: 舊的 3 個被刪，新的與 cron.log 仍在
    assert not (daily_dir / "stt_2026-09-10.log").exists()
    assert not (daily_dir / "2026-09-10.log").exists()
    assert not (daily_dir / "topic_stats_2026-09-10.json").exists()
    assert (daily_dir / "2026-09-26.log").exists()
    assert (daily_dir / "review_cron.log").exists()

    # 驗證 presence: 產生了 .bak，且原檔只留 1 行未過期的
    baks = list(tmp_path.glob("voice_presence.jsonl.bak_*"))
    assert len(baks) == 1
    assert len(baks[0].read_text().strip().splitlines()) == 2
    lines = presence_file.read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "leave"


def test_prune_retention_sandbox_active(tmp_path, monkeypatch):
    """沙盒 active 時即使帶 --apply 也必須是 no-op。"""
    from scripts.prune_retention import prune_all
    db_file = tmp_path / "test_marvin.db"
    _create_schema(db_file)
    now = 1790800000.0
    conn = sqlite3.connect(db_file)
    conn.execute(
        "INSERT INTO speaker_topic_graph (speaker, channel_id, text, created_at) VALUES ('Alice', 1, 'old topic', ?)",
        (now - 40 * 86400,),
    )
    conn.commit()
    conn.close()

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    presence_file = tmp_path / "voice_presence.jsonl"
    presence_file.write_text(json.dumps({"ts": now - 100 * 86400, "user_id": "1", "event": "join"}) + "\n")

    monkeypatch.setattr(memory_sandbox, "active", lambda: True)

    summary = prune_all(
        db_path=db_file,
        daily_dir=daily_dir,
        presence_path=presence_file,
        now=now,
        apply=True,
    )
    assert summary["dry_run"] is True or summary["speaker_topic_graph"]["deleted"] == 0

    conn = sqlite3.connect(db_file)
    assert conn.execute("SELECT count(*) FROM speaker_topic_graph").fetchone()[0] == 1
    conn.close()

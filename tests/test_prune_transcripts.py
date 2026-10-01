"""測試 scripts/prune_transcripts.py 的 dry-run 與 --apply。"""
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from transcript_store import TranscriptStore


def test_prune_transcripts_dry_run_and_apply(tmp_path):
    db_file = tmp_path / "test_marvin.db"
    store = TranscriptStore(db_path=str(db_file))

    now = time.time()
    old_ts = now - 20 * 86400  # 20 天前（超過 14 天）
    new_ts = now - 5 * 86400   # 5 天前（未超過 14 天）

    conn = sqlite3.connect(db_file)
    conn.execute(
        "INSERT INTO transcripts (speaker, guild_id, channel_id, text, timestamp) VALUES ('Alice', 100, 1, 'old text', ?)",
        (old_ts,),
    )
    conn.execute(
        "INSERT INTO transcripts (speaker, guild_id, channel_id, text, timestamp) VALUES ('Bob', 100, 1, 'new text', ?)",
        (new_ts,),
    )
    conn.commit()
    conn.close()

    script_path = Path(__file__).resolve().parent.parent / "scripts" / "prune_transcripts.py"

    # 1. dry-run
    res_dry = subprocess.run(
        [sys.executable, str(script_path), "--db", str(db_file)],
        capture_output=True,
        text=True,
        check=True,
    )
    out_dry = json.loads(res_dry.stdout.strip())
    assert out_dry["dry_run"] is True
    assert out_dry["matched_rows"] == 1
    assert "oldest_timestamp" in out_dry

    # 檢查 DB 資料完全沒被刪除
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM transcripts")
    assert cur.fetchone()[0] == 2
    conn.close()

    # 2. apply
    res_apply = subprocess.run(
        [sys.executable, str(script_path), "--db", str(db_file), "--apply"],
        capture_output=True,
        text=True,
        check=True,
    )
    out_apply = json.loads(res_apply.stdout.strip())
    assert out_apply["dry_run"] is False
    assert out_apply["deleted_rows"] == 1

    # 檢查 DB 資料只剩下 1 筆未過期的
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    cur.execute("SELECT text FROM transcripts")
    rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "new text"
    conn.close()

"""測試 scripts/prune_presence_log.py 清理工具。

涵蓋：
- 預設 dry-run 不改動原檔，印出統計
- --apply 模式備份為 .bak_<YYYYMMDD>，並過濾掉 bot、未同意者、move
"""
import json
import subprocess
import sys
from pathlib import Path


def test_prune_presence_log_dry_run_and_apply(tmp_path):
    log_file = tmp_path / "voice_presence.jsonl"
    consent_file = tmp_path / "consent.json"

    consent_data = {
        "consented": {
            "Alice": True,
            "Bob": False,
        }
    }
    consent_file.write_text(json.dumps(consent_data), encoding="utf-8")

    records = [
        {"ts": 1.0, "user_name": "Alice", "is_bot": False, "event": "join", "channel_id": "1"},
        {"ts": 2.0, "user_name": "Alice", "is_bot": False, "event": "leave", "channel_id": "1"},
        {"ts": 3.0, "user_name": "Alice", "is_bot": False, "event": "move", "channel_id": "2"}, # move -> remove
        {"ts": 4.0, "user_name": "BotUser", "is_bot": True, "event": "join", "channel_id": "1"}, # bot -> remove
        {"ts": 5.0, "user_name": "Bob", "is_bot": False, "event": "join", "channel_id": "1"}, # unconsented -> remove
        {"ts": 6.0, "user_name": "Charlie", "is_bot": False, "event": "join", "channel_id": "1"}, # unconsented -> remove
    ]
    log_file.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    script_path = Path(__file__).resolve().parent.parent / "scripts" / "prune_presence_log.py"

    # 1. 跑 dry-run
    res_dry = subprocess.run(
        [sys.executable, str(script_path), "--presence", str(log_file), "--consent", str(consent_file)],
        capture_output=True,
        text=True,
        check=True,
    )
    stdout_dry = res_dry.stdout
    assert "總行數: 6" in stdout_dry or "6" in stdout_dry
    # 原檔內容不變
    assert len(log_file.read_text(encoding="utf-8").strip().splitlines()) == 6
    # 沒有產生 .bak 檔
    baks = list(tmp_path.glob("voice_presence.jsonl.bak_*"))
    assert len(baks) == 0

    # 2. 跑 --apply
    res_apply = subprocess.run(
        [sys.executable, str(script_path), "--presence", str(log_file), "--consent", str(consent_file), "--apply"],
        capture_output=True,
        text=True,
        check=True,
    )
    # 有產生 .bak 檔
    baks = list(tmp_path.glob("voice_presence.jsonl.bak_*"))
    assert len(baks) == 1
    bak_lines = baks[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(bak_lines) == 6

    # 新檔案只剩下已同意且非 bot 的 join/leave (只有第 1, 2 筆 Alice)
    new_lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(new_lines) == 2
    parsed = [json.loads(line) for line in new_lines]
    assert [p["ts"] for p in parsed] == [1.0, 2.0]

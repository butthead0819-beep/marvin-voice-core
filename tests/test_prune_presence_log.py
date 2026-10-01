"""測試 scripts/prune_presence_log.py 清理工具。

涵蓋：
- 預設 dry-run 不改動原檔，印出統計
- --apply 模式備份為 .bak_<YYYYMMDD>，並過濾掉 bot、未同意者、move、非 Marvin 頻道
- --marvin-user-id 頻道過濾與 other_channel 統計
"""
import json
import subprocess
import sys
from pathlib import Path
import pytest
from scripts.prune_presence_log import prune_presence


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
        {"ts": 1.0, "user_id": "1001", "user_name": "Alice", "is_bot": False, "event": "join", "channel_id": "1"},
        {"ts": 2.0, "user_id": "1001", "user_name": "Alice", "is_bot": False, "event": "leave", "channel_id": "1"},
        {"ts": 3.0, "user_id": "1001", "user_name": "Alice", "is_bot": False, "event": "move", "channel_id": "1"}, # move -> remove
        {"ts": 4.0, "user_id": "9999", "user_name": "MarvinBot", "is_bot": True, "event": "join", "channel_id": "1"}, # marvin bot in ch 1
        {"ts": 5.0, "user_id": "1002", "user_name": "Bob", "is_bot": False, "event": "join", "channel_id": "1"}, # unconsented -> remove
        {"ts": 6.0, "user_id": "1003", "user_name": "Charlie", "is_bot": False, "event": "join", "channel_id": "1"}, # unconsented -> remove
    ]
    log_file.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    script_path = Path(__file__).resolve().parent.parent / "scripts" / "prune_presence_log.py"

    # 1. 跑 dry-run (帶 --marvin-user-id 9999)
    res_dry = subprocess.run(
        [sys.executable, str(script_path), "--presence", str(log_file), "--consent", str(consent_file), "--marvin-user-id", "9999"],
        capture_output=True,
        text=True,
        check=True,
    )
    stdout_dry = res_dry.stdout
    assert "總行數: 6" in stdout_dry or "6" in stdout_dry
    assert len(log_file.read_text(encoding="utf-8").strip().splitlines()) == 6
    baks = list(tmp_path.glob("voice_presence.jsonl.bak_*"))
    assert len(baks) == 0

    # 2. 跑 --apply
    res_apply = subprocess.run(
        [sys.executable, str(script_path), "--presence", str(log_file), "--consent", str(consent_file), "--marvin-user-id", "9999", "--apply"],
        capture_output=True,
        text=True,
        check=True,
    )
    baks = list(tmp_path.glob("voice_presence.jsonl.bak_*"))
    assert len(baks) == 1
    bak_lines = baks[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(bak_lines) == 6

    # 新檔案只剩下已同意且非 bot、在 Marvin 頻道的 join/leave (只有第 1, 2 筆 Alice)
    new_lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(new_lines) == 2
    parsed = [json.loads(line) for line in new_lines]
    assert [p["ts"] for p in parsed] == [1.0, 2.0]


def test_prune_presence_channel_filter(tmp_path):
    log_file = tmp_path / "voice_presence.jsonl"
    consent_file = tmp_path / "consent.json"

    consent_file.write_text(json.dumps({"consented": {"Alice": True}}), encoding="utf-8")

    records = [
        # Marvin 曾進過頻道 A (ch_a)
        {"ts": 1.0, "user_id": "9999", "user_name": "MarvinBot", "is_bot": True, "event": "join", "channel_id": "ch_a"},
        # Alice 在頻道 A (保留)
        {"ts": 2.0, "user_id": "1001", "user_name": "Alice", "is_bot": False, "event": "join", "channel_id": "ch_a"},
        # Alice 在頻道 B (非 Marvin 頻道，丟掉)
        {"ts": 3.0, "user_id": "1001", "user_name": "Alice", "is_bot": False, "event": "join", "channel_id": "ch_b"},
    ]
    log_file.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    res = prune_presence(log_file, consent_file, marvin_user_id="9999", apply=True)
    assert res["total"] == 3
    assert res["bot"] == 1
    assert res["other_channel"] == 1
    assert res["retained"] == 1

    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["channel_id"] == "ch_a"


def test_prune_presence_empty_marvin_channels(tmp_path):
    """若 Marvin 沒有任何頻道紀錄，保守起見所有人類行都算非 Marvin 頻道，不保留。"""
    log_file = tmp_path / "voice_presence.jsonl"
    consent_file = tmp_path / "consent.json"

    consent_file.write_text(json.dumps({"consented": {"Alice": True}}), encoding="utf-8")

    records = [
        {"ts": 1.0, "user_id": "1001", "user_name": "Alice", "is_bot": False, "event": "join", "channel_id": "ch_a"},
    ]
    log_file.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    res = prune_presence(log_file, consent_file, marvin_user_id="9999", apply=False)
    assert res["total"] == 1
    assert res["other_channel"] == 1
    assert res["retained"] == 0

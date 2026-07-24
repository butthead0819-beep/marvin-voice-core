"""
tests/test_scan_claude_sessions.py
TDD：scripts/scan_claude_sessions.py — 掃 ~/.claude/sessions/*.json（存活 session 登記檔）
+ ~/.claude/projects/<slug>/<sessionId>.jsonl（transcript），判斷「輪到你回應了嗎」。

判準（已跟使用者確認，粗粒度但夠用）：最後一則訊息是 assistant，且
（transcript 檔案一段時間沒新寫入 OR pid 已消失）→ waiting=True。
"""
import json
import os
import time

import pytest

from scripts.scan_claude_sessions import scan_sessions


def _write_session_json(sessions_dir, pid, session_id, cwd):
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / f"{pid}.json").write_text(json.dumps({
        "pid": pid, "sessionId": session_id, "cwd": cwd,
        "kind": "interactive",
    }), encoding="utf-8")


def _write_transcript(projects_dir, cwd, session_id, entries, mtime=None):
    slug = cwd.replace("/", "-")
    proj_dir = projects_dir / slug
    proj_dir.mkdir(parents=True, exist_ok=True)
    path = proj_dir / f"{session_id}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _assistant_entry(text):
    return {"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "text", "text": text}]}}


def _user_entry(text):
    return {"type": "user", "message": {"role": "user",
            "content": [{"type": "text", "text": text}]}}


def test_waiting_true_when_last_message_assistant_and_idle(tmp_path):
    sessions_dir = tmp_path / "sessions"
    projects_dir = tmp_path / "projects"
    cwd = "/Users/jackhuang/Code/Discord-voice-bot"
    pid = os.getpid()  # 存活 pid
    _write_session_json(sessions_dir, pid, "sess-1", cwd)
    now = time.time()
    _write_transcript(projects_dir, cwd, "sess-1",
                       [_user_entry("幫我看一下"), _assistant_entry("要不要重跑 CI？")],
                       mtime=now - 100)

    results = scan_sessions(str(sessions_dir), str(projects_dir), now=now, idle_threshold_s=15)

    assert len(results) == 1
    r = results[0]
    assert r["session_id"] == "sess-1"
    assert r["cwd"] == cwd
    assert r["waiting"] is True
    assert r["last_text"] == "要不要重跑 CI？"


def test_waiting_false_when_last_message_is_user(tmp_path):
    sessions_dir = tmp_path / "sessions"
    projects_dir = tmp_path / "projects"
    cwd = "/Users/jackhuang/Code/x"
    pid = os.getpid()
    _write_session_json(sessions_dir, pid, "sess-2", cwd)
    now = time.time()
    _write_transcript(projects_dir, cwd, "sess-2",
                       [_assistant_entry("好"), _user_entry("再幫我做另一件事")],
                       mtime=now - 100)

    results = scan_sessions(str(sessions_dir), str(projects_dir), now=now, idle_threshold_s=15)

    assert results[0]["waiting"] is False


def test_waiting_false_when_assistant_message_still_streaming_recently(tmp_path):
    """檔案剛寫入（還在生成中）不算等待——避免把正在跑的 session 誤標成等你回應。"""
    sessions_dir = tmp_path / "sessions"
    projects_dir = tmp_path / "projects"
    cwd = "/Users/jackhuang/Code/y"
    pid = os.getpid()
    _write_session_json(sessions_dir, pid, "sess-3", cwd)
    now = time.time()
    _write_transcript(projects_dir, cwd, "sess-3",
                       [_assistant_entry("正在想")], mtime=now - 1)

    results = scan_sessions(str(sessions_dir), str(projects_dir), now=now, idle_threshold_s=15)

    assert results[0]["waiting"] is False


def test_waiting_true_when_pid_already_dead_regardless_of_mtime(tmp_path):
    sessions_dir = tmp_path / "sessions"
    projects_dir = tmp_path / "projects"
    cwd = "/Users/jackhuang/Code/z"
    dead_pid = 999999999  # 不太可能存在的 pid
    _write_session_json(sessions_dir, dead_pid, "sess-4", cwd)
    now = time.time()
    _write_transcript(projects_dir, cwd, "sess-4",
                       [_assistant_entry("剛講完就死了")], mtime=now - 1)

    results = scan_sessions(str(sessions_dir), str(projects_dir), now=now, idle_threshold_s=15)

    assert results[0]["waiting"] is True


def test_skips_meta_entries_without_message_role(tmp_path):
    """last-prompt / ai-title 這種 meta 行沒有 message.role，不能被當成「最後一則」。"""
    sessions_dir = tmp_path / "sessions"
    projects_dir = tmp_path / "projects"
    cwd = "/Users/jackhuang/Code/meta"
    pid = os.getpid()
    _write_session_json(sessions_dir, pid, "sess-5", cwd)
    now = time.time()
    _write_transcript(projects_dir, cwd, "sess-5", [
        _assistant_entry("要不要重跑？"),
        {"type": "last-prompt", "lastPrompt": "x", "sessionId": "sess-5"},
        {"type": "ai-title", "aiTitle": "y", "sessionId": "sess-5"},
    ], mtime=now - 100)

    results = scan_sessions(str(sessions_dir), str(projects_dir), now=now, idle_threshold_s=15)

    assert results[0]["waiting"] is True
    assert results[0]["last_text"] == "要不要重跑？"


def test_returns_empty_list_when_sessions_dir_missing(tmp_path):
    results = scan_sessions(str(tmp_path / "no_such_dir"), str(tmp_path / "projects"))
    assert results == []


def test_missing_transcript_file_is_skipped_not_crashed(tmp_path):
    sessions_dir = tmp_path / "sessions"
    projects_dir = tmp_path / "projects"
    _write_session_json(sessions_dir, os.getpid(), "sess-ghost", "/Users/jackhuang/Code/ghost")
    results = scan_sessions(str(sessions_dir), str(projects_dir))
    assert results == []

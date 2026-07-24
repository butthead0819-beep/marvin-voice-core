"""scripts/scan_claude_sessions.py — 掃這台 Mac 上所有 Claude Code session，寫進
claude_sessions_state.py 橋接檔，給 HUD /claude_status 讀（見 main_satellite.py）。

資料來源：
- ~/.claude/sessions/*.json：存活 session 登記檔（pid/sessionId/cwd）。
- ~/.claude/projects/<cwd 轉 slug>/<sessionId>.jsonl：該 session 的 transcript。

判準（粗粒度、跟使用者確認過）：transcript 最後一則有 message.role 的訊息是
assistant，且（transcript 檔案一段時間沒新寫入 OR pid 已消失）→ waiting=True，
「結論/next action」直接取那則 assistant 訊息的文字，不另外呼叫 LLM 摘要
（避免二次摘要因 context 不足失真）。
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import sys
import time
from typing import Awaitable, Callable

DEFAULT_SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
DEFAULT_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
IDLE_THRESHOLD_S = 15.0
LAST_TEXT_MAX_CHARS = 400


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但不是我們的 process，仍算活著
    return True


def _extract_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "".join(parts)
    return ""


def _last_role_and_text(transcript_path: str) -> tuple[str | None, str]:
    last_role, last_text = None, ""
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message = entry.get("message")
                if not isinstance(message, dict) or "role" not in message:
                    continue  # meta 行（last-prompt/ai-title...）沒有 message.role
                last_role = message["role"]
                last_text = _extract_text(message)
    except FileNotFoundError:
        return None, ""
    return last_role, last_text


def scan_sessions(sessions_dir: str = DEFAULT_SESSIONS_DIR,
                   projects_dir: str = DEFAULT_PROJECTS_DIR, *,
                   now: float | None = None,
                   idle_threshold_s: float = IDLE_THRESHOLD_S) -> list[dict]:
    if now is None:
        now = time.time()
    results = []
    for path in sorted(glob.glob(os.path.join(sessions_dir, "*.json"))):
        try:
            with open(path, "r", encoding="utf-8") as f:
                reg = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        pid, session_id, cwd = reg.get("pid"), reg.get("sessionId"), reg.get("cwd")
        if not session_id or not cwd:
            continue
        slug = cwd.replace("/", "-")
        transcript_path = os.path.join(projects_dir, slug, f"{session_id}.jsonl")
        if not os.path.exists(transcript_path):
            continue
        last_role, last_text = _last_role_and_text(transcript_path)
        if last_role is None:
            continue
        mtime = os.path.getmtime(transcript_path)
        pid_dead = pid is not None and not _pid_alive(pid)
        idle = pid_dead or (now - mtime) > idle_threshold_s
        waiting = last_role == "assistant" and idle
        results.append({
            "session_id": session_id,
            "project": os.path.basename(cwd),
            "cwd": cwd,
            "waiting": waiting,
            "last_text": last_text[:LAST_TEXT_MAX_CHARS],
            "updated_at": mtime,
        })
    return results


async def run_claude_sessions_scan_loop(
    *,
    sessions_dir: str = DEFAULT_SESSIONS_DIR,
    projects_dir: str = DEFAULT_PROJECTS_DIR,
    state_path: str | None = None,
    interval_s: float = 20.0,
    scan_fn: Callable[[str, str], list] = scan_sessions,
    save_fn: Callable[..., None] | None = None,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    should_stop: Callable[[], bool] | None = None,
) -> None:
    """背景迴圈：週期性掃 Claude Code session 狀態寫進橋接檔（比照 car_mode.run_car_ttl_loop）。

    一拍失敗（讀檔壞掉、格式跑掉）不弄垮迴圈，下一拍繼續。
    """
    if save_fn is None:
        from claude_sessions_state import DEFAULT_PATH, save_claude_sessions_state
        save_fn = save_claude_sessions_state
        state_path = state_path or DEFAULT_PATH
    while should_stop is None or not should_stop():
        try:
            sessions = scan_fn(sessions_dir, projects_dir)
            save_fn(sessions=sessions, path=state_path)
        except Exception:  # noqa: BLE001
            pass
        await sleep_fn(interval_s)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions-dir", default=DEFAULT_SESSIONS_DIR)
    parser.add_argument("--projects-dir", default=DEFAULT_PROJECTS_DIR)
    parser.add_argument("--state-path", default=None)
    args = parser.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from claude_sessions_state import DEFAULT_PATH, save_claude_sessions_state

    sessions = scan_sessions(args.sessions_dir, args.projects_dir)
    save_claude_sessions_state(sessions=sessions, path=args.state_path or DEFAULT_PATH)
    print(f"[scan_claude_sessions] wrote {len(sessions)} session(s)")


if __name__ == "__main__":
    main()

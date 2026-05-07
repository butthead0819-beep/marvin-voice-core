#!/bin/bash
# Wrapper for analyze_daily_log.py called by launchd.
# Python can get EINTR (errno 4) during startup when launched directly by launchd
# right after system wake. Running via bash avoids that and gives us retry logic.

export HOME="/Users/jackhuang"
export PATH="/Users/jackhuang/Documents/Antigravity/Discord-voice-bot/venv_simon/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1

PYTHON="/Users/jackhuang/Documents/Antigravity/Discord-voice-bot/venv_simon/bin/python3"
SCRIPT="/Users/jackhuang/Documents/Antigravity/Discord-voice-bot/scripts/analyze_daily_log.py"
WORKDIR="/Users/jackhuang/Documents/Antigravity/Discord-voice-bot"

cd "$WORKDIR" || exit 1

sleep 5  # let the system settle after wake/launchd scheduling

for attempt in 1 2 3; do
    echo "[run_daily_review] attempt $attempt starting at $(date)..."
    "$PYTHON" "$SCRIPT" && exit 0
    echo "[run_daily_review] attempt $attempt failed (exit $?), retrying in 15s..."
    sleep 15
done

echo "[run_daily_review] all 3 attempts failed at $(date)"
exit 1

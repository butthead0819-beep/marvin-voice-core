#!/bin/bash
# Wrapper for slice_stt_daily.py called by launchd.
# Mirrors run_daily_review.sh: sets env, retries on EINTR crash.

export HOME="/Users/jackhuang"
export PATH="/Users/jackhuang/Code/Discord-voice-bot/venv_simon/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1

PYTHON="/Users/jackhuang/Code/Discord-voice-bot/venv_simon/bin/python3"
SCRIPT="/Users/jackhuang/Code/Discord-voice-bot/scripts/slice_stt_daily.py"
WORKDIR="/Users/jackhuang/Code/Discord-voice-bot"

cd "$WORKDIR" || exit 1

sleep 3

for attempt in 1 2 3; do
    echo "[run_daily_slice] attempt $attempt starting at $(date)..."
    "$PYTHON" "$SCRIPT" && exit 0
    echo "[run_daily_slice] attempt $attempt failed (exit $?), retrying in 15s..."
    sleep 15
done

echo "[run_daily_slice] all 3 attempts failed at $(date)"
exit 1

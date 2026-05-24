#!/bin/bash
# Nightly wrapper for analyze_daily_feedback.py called by launchd at 04:00.
#
# Same pre-warm/retry pattern as run_daily_review.sh — workaround for macOS
# launchd EINTR during Python first init.

echo "[run_nightly_feedback] === bash entered at $(date) ==="

export HOME="/Users/jackhuang"
export PATH="/Users/jackhuang/Code/Discord-voice-bot/venv_simon/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1

PYTHON="/Users/jackhuang/Code/Discord-voice-bot/venv_simon/bin/python3"
SCRIPT="/Users/jackhuang/Code/Discord-voice-bot/scripts/analyze_daily_feedback.py"
WORKDIR="/Users/jackhuang/Code/Discord-voice-bot"

cd "$WORKDIR" || exit 1

# Source .env for GROQ_API_KEY etc
if [ -f .env ]; then
    set -a; source .env; set +a
fi

# Yesterday's local date — analyze yesterday's recs each night at 04:00
YESTERDAY=$(date -v-1d "+%Y-%m-%d" 2>/dev/null || date -d "yesterday" "+%Y-%m-%d")

echo "[run_nightly_feedback] 🕐 target date: ${YESTERDAY}, starting at $(date)"

# Pre-warm: absorb EINTR from launchd init signals
prewarm_ok=0
for pw in $(seq 1 10); do
    if "$PYTHON" -c 'import sys; sys.exit(0)' 2>/dev/null; then
        echo "[run_nightly_feedback] ✅ pre-warm OK after ${pw} attempt(s)"
        prewarm_ok=1
        break
    fi
    echo "[run_nightly_feedback] ⚡ pre-warm attempt ${pw} got EINTR, retrying in 3s..."
    sleep 3
done

if [ "$prewarm_ok" -eq 0 ]; then
    echo "[run_nightly_feedback] ❌ pre-warm failed after 10 attempts — aborting"
    exit 1
fi

sleep 2

for attempt in 1 2 3; do
    echo "[run_nightly_feedback] 🚀 attempt ${attempt} at $(date)..."
    "$PYTHON" "$SCRIPT" "$YESTERDAY" && {
        echo "[run_nightly_feedback] ✅ completed at $(date)"
        exit 0
    }
    EXIT_CODE=$?
    echo "[run_nightly_feedback] ⚠ attempt ${attempt} failed (exit ${EXIT_CODE}), retrying in 60s..."
    sleep 60
done

echo "[run_nightly_feedback] ❌ all 3 attempts failed at $(date)"
exit 1

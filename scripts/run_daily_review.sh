#!/bin/bash
# Wrapper for analyze_daily_log.py called by launchd.
#
# macOS launchd sends signals (SIGCHLD/SIGPIPE) to spawned processes during
# Python's <frozen getpath> initialization, causing InterruptedError before
# any user code runs. Workaround: detach from launchd's session via setsid,
# which prevents signal inheritance and lets Python init cleanly.

echo "[run_daily_review] === bash entered at $(date) ==="

export HOME="/Users/jackhuang"
export PATH="/Users/jackhuang/Documents/Antigravity/Discord-voice-bot/venv_simon/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1

PYTHON="/Users/jackhuang/Documents/Antigravity/Discord-voice-bot/venv_simon/bin/python3"
SCRIPT="/Users/jackhuang/Documents/Antigravity/Discord-voice-bot/scripts/analyze_daily_log.py"
WORKDIR="/Users/jackhuang/Documents/Antigravity/Discord-voice-bot"

cd "$WORKDIR" || exit 1

echo "[run_daily_review] 🕐 starting at $(date)"

# Pre-warm: absorb the EINTR that launchd sends during Python's first init.
# Run up to 10 trivial invocations until one succeeds, then proceed.
prewarm_ok=0
for pw in $(seq 1 10); do
    if "$PYTHON" -c 'import sys; sys.exit(0)' 2>/dev/null; then
        echo "[run_daily_review] ✅ pre-warm OK after ${pw} attempt(s)"
        prewarm_ok=1
        break
    fi
    echo "[run_daily_review] ⚡ pre-warm attempt ${pw} got EINTR, retrying in 3s..."
    sleep 3
done

if [ "$prewarm_ok" -eq 0 ]; then
    echo "[run_daily_review] ❌ pre-warm failed after 10 attempts — aborting"
    exit 1
fi

sleep 2  # brief pause after pre-warm before the real run

for attempt in 1 2 3; do
    echo "[run_daily_review] 🚀 attempt ${attempt} at $(date)..."
    "$PYTHON" "$SCRIPT" && {
        echo "[run_daily_review] ✅ completed at $(date)"
        exit 0
    }
    EXIT_CODE=$?
    echo "[run_daily_review] ⚠ attempt ${attempt} failed (exit ${EXIT_CODE}), retrying in 30s..."
    sleep 30
done

echo "[run_daily_review] ❌ all 3 attempts failed at $(date)"
exit 1

#!/usr/bin/env python3
"""
IBA Daily Calibration Script
Reads records/daily/*.log → recomputes non-voice channel weights → saves records/iba_calibration.json

Run manually:  python iba_calibrate.py
Or add to cron: 0 12 * * * cd /path/to/bot && python iba_calibrate.py >> records/daily/slice_cron.log 2>&1
"""

import sys, logging
from pathlib import Path
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

sys.path.insert(0, str(Path(__file__).parent))
from wake_signal_fusion import WakeSignalFusion

def main():
    f = WakeSignalFusion()
    result = f.calibrate_from_logs()
    if not result:
        print("⚠️  Not enough data for calibration — no changes made.")
        return

    w = result["non_voice_weights"]
    d = result["discriminability"]
    print(f"\n{'='*55}")
    print(f"  IBA Calibration — {result['saved_at']}")
    print(f"{'='*55}")
    print(f"  Corpus: pos={result['corpus_pos']}  neg={result['corpus_neg']}  ambig={result['corpus_ambig']}")
    print()
    print(f"  Channel       disc    old_w   new_w")
    print(f"  {'─'*42}")
    old = {"task": 0.22, "info": 0.04, "control": 0.24}
    for k in ("task", "info", "control"):
        arrow = "↑" if w[k] > old[k]+0.005 else ("↓" if w[k] < old[k]-0.005 else "→")
        print(f"  {k:10s}  {d[k]:+.3f}   {old[k]:.3f}   {w[k]:.4f}  {arrow}")
    print(f"\n  Saved → records/iba_calibration.json")

if __name__ == "__main__":
    main()

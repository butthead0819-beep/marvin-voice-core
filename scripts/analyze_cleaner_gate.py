"""回溯量測：本地 cleaner pre-gate 會省多少呼叫、會不會漏接真 wake。

不用等 live shadow——直接拿歷史 log 跑 cleaner_gate_decision：
  (Debounced) <raw>           = 每一句 STT（含環境閒聊）→ drop-rate（潛在省下）
  [⚡喚醒] raw='<raw>'          = 喚醒偵測到的 → false-neg（gate 會丟掉的真 wake）
  [✅Query通過] query='<q>'     = 真的派發的指令 → 最強 false-neg（不該丟）

注意：context_active / marvin_just_spoke 不在 log 裡 → 一律當 False（保守）。
這讓 gate 更嚴（丟更多）→ drop-rate 與 false-neg 都是「上界/最壞情況」；
若這裡 false-neg 可接受，live（有對話 bypass）只會更好。

用法：python scripts/analyze_cleaner_gate.py bot_main.log* records/daily/*.log
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from stt_cleaner import cleaner_gate_decision  # noqa: E402

_DEBOUNCED_RE = re.compile(r"\(Debounced\)\s+(\S.*?)\s*$")
_WAKE_RE = re.compile(r"\[⚡喚醒\] \[[^\]]+\] raw='([^']*)'")
_QUERY_OK_RE = re.compile(r"\[✅Query通過\] \[[^\]]+\] gate_ok \| query='([^']*)'")


def _collect(paths):
    debounced, wakes, queries = [], [], []
    for p in paths:
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _DEBOUNCED_RE.search(line)
                if m:
                    debounced.append(m.group(1).strip())
                    continue
                m = _WAKE_RE.search(line)
                if m:
                    wakes.append(m.group(1))
                    continue
                m = _QUERY_OK_RE.search(line)
                if m:
                    queries.append(m.group(1))
    return debounced, wakes, queries


def _gate_drop(raw: str) -> bool:
    would_send, _ = cleaner_gate_decision(raw, context_active=False, marvin_just_spoke=False)
    return not would_send


def _pct(n, d):
    return f"{n}/{d} ({(n / d * 100) if d else 0:.1f}%)"


def main():
    paths = sys.argv[1:]
    if not paths:
        print(__doc__)
        sys.exit(1)
    debounced, wakes, queries = _collect(paths)
    # 去重看 distinct 行為，但 drop-rate 用全量（反映實際呼叫頻率）
    deb_drop = [r for r in debounced if _gate_drop(r)]
    wake_drop = [r for r in wakes if _gate_drop(r)]
    q_drop = [r for r in queries if _gate_drop(r)]

    print("════════════════════════════════════════════")
    print(f"讀 {len(paths)} 路徑")
    print(f"(Debounced) 總句數: {len(debounced)}")
    print(f"[⚡喚醒] 喚醒偵測句數: {len(wakes)}")
    print(f"[✅Query通過] 派發指令句數: {len(queries)}")
    print("────────────────────────────────────────────")
    print(f"DROP-RATE（gate 會略過的句子，= 省下的 cleaner 呼叫）: {_pct(len(deb_drop), len(debounced))}")
    print(f"FALSE-NEG（喚醒偵測句被 gate 丟掉）: {_pct(len(wake_drop), len(wakes))}   ← 越低越好")
    print(f"FALSE-NEG（派發指令句被 gate 丟掉）: {_pct(len(q_drop), len(queries))}   ← 必須趨近 0")
    print("════════════════════════════════════════════")
    if wake_drop:
        print("\n⚠️ 被 gate 丟掉的喚醒句（要靠這些反推 wake 音標集要多寬）：")
        seen = set()
        for r in wake_drop:
            if r in seen:
                continue
            seen.add(r)
            print(f"   '{r[:50]}'")
            if len(seen) >= 30:
                break
    if q_drop:
        print("\n🔴 被 gate 丟掉的『已派發指令』（絕不該丟，gate 必須涵蓋）：")
        for r in dict.fromkeys(q_drop):
            print(f"   '{r[:50]}'")


if __name__ == "__main__":
    main()

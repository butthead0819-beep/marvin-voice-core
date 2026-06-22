"""驗證 A：讀 records/laugh_rhythm_probe.jsonl（live 就地算好的節律數字），
看「笑」vs「講話」在 (peaks_per_sec, regularity) 上分不分得開。

若兩群明顯分開 → A 可行、門檻可定；混在一起 → A 在這音訊上不可靠。
不碰任何音訊（驗證資料只有數字 + STT 弱標籤）。

用法：venv_simon/bin/python scripts/laugh_envelope_probe.py
"""
import json
import os
import sys

sys.path.insert(0, ".")
from laugh_acoustics import looks_like_laugh

LOG = "records/laugh_rhythm_probe.jsonl"


def _summary(name, rows):
    if not rows:
        print(f"  {name}: （無樣本）")
        return
    pps = sorted(r["pps"] for r in rows)
    reg = sorted(r["reg"] for r in rows)
    hit = sum(1 for r in rows if looks_like_laugh(
        {"bursts": r["bursts"], "peaks_per_sec": r["pps"], "regularity": r["reg"]}))
    med = lambda xs: xs[len(xs) // 2]
    print(f"  {name}: n={len(rows)}  pps 中位={med(pps):.1f} [{pps[0]:.1f}–{pps[-1]:.1f}]"
          f"  reg 中位={med(reg):.2f}  判定為笑={hit}/{len(rows)}")


def main():
    if not os.path.exists(LOG):
        print("沒有資料。先設 LAUGH_RHYTHM_LOG=1 重啟 bot、跑一場有笑的對話，再執行本腳本。")
        return
    laughs, speeches = [], []
    with open(LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            (laughs if r.get("label") == "laugh" else speeches).append(r)

    print(f"樣本 {len(laughs)+len(speeches)} 筆（笑錨 {len(laughs)} / 講話錨 {len(speeches)}）\n")
    print("== 分群統計（看兩群分不分得開）==")
    _summary("笑 (STT 清楚哈哈)", laughs)
    _summary("講話 (STT 長句)", speeches)
    if speeches:
        fp = sum(1 for r in speeches if looks_like_laugh(
            {"bursts": r["bursts"], "peaks_per_sec": r["pps"], "regularity": r["reg"]})) / len(speeches)
        print(f"\n假陽性（講話被判成笑）: {fp*100:.0f}%　→ 越低越好")
    if laughs:
        tp = sum(1 for r in laughs if looks_like_laugh(
            {"bursts": r["bursts"], "peaks_per_sec": r["pps"], "regularity": r["reg"]})) / len(laughs)
        print(f"真陽性（笑被判成笑）: {tp*100:.0f}%　→ 越高越好")


if __name__ == "__main__":
    main()

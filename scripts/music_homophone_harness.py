#!/usr/bin/env python3
"""驗證 fast-path 可行性：本地歌表（拼音）fuzzy 能否安全解中文同音字糊字。

載入 build_music_catalog.py 產的乾淨目錄，拿擴大標註樣本掃門檻，量：
  - recall：在目錄裡的歌，正解 rank#1 且 ≥門檻
  - false-match：不在目錄的 query（亂碼/非歌）卻 ≥門檻命中錯歌（fast-path 最怕這個）
比較 char vs 拼音 scorer，找「recall 高且 false-match≈0」的安全門檻。

依賴 rapidfuzz + pypinyin（harness 用 /tmp venv；prod 要正式加 dep）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from rapidfuzz import fuzz, process
from pypinyin import lazy_pinyin

CATALOG = Path("records/music_catalog.json")

# (query, expected)：expected=正解須含的鑑別字串；None=該 query 不該命中任何歌（拒絕測試）
# 來源：judge_outcomes 同音糊字 + stt_history 真實高頻點歌 query
LABELED = [
    # ── 同音字糊字（核心測試）──
    ("官者的想你的夜", "想你的夜"),
    ("陶喆的月亮錶是誰的心", "月亮代表"),
    ("陶喆的書上an說", "Susan"),        # 英文名同音極限
    ("張惠妹的如果你也還聽說", "如果你也聽說"),
    ("陳華的陪你去看五月的晚霞", "晚霞"),
    # ── 真實高頻點歌（剝喚醒詞後）──
    ("想你的夜", "想你的夜"),
    ("關喆想你的夜", "想你的夜"),
    ("七里香", "七里香"),
    ("周杰倫的簡單愛", "簡單愛"),
    ("周杰倫的晴天", "晴天"),
    ("周杰倫的夜曲", "夜曲"),
    ("信樂團的離歌", "離歌"),
    ("伍佰的牽掛", "牽掛"),
    ("海闊天空", "海闊天空"),
    ("林宥嘉的傻子", "傻子"),
    ("方大同的紅豆", "紅豆"),
    ("蔡依林的倒帶", "倒帶"),
    ("莫文蔚的慢慢喜歡你", "慢慢喜歡你"),
    ("滅火器的島嶼天光", "島嶼天光"),
    ("盧廣仲的早安晨之美", "早安晨之美"),
    ("我的歌聲裡", "我的歌聲裡"),
    ("陳奕迅的我們", "我們"),
    ("費玉清的晚安曲", "晚安曲"),
    ("蔡健雅的達爾文", "達爾文"),
    ("陶喆的王八蛋送給阿文", "王八蛋"),
    # ── 拒絕案例（不在目錄/非歌）──
    ("播放完全不存在的亂碼歌zzz", None),
    ("今天天氣真好啊", None),
    ("幫我把電燈關掉", None),
]

THRESHOLDS = [70, 75, 80, 85, 90]


def to_pinyin(s: str) -> str:
    return " ".join(lazy_pinyin(s)).lower()


def load_catalog():
    cat = json.loads(CATALOG.read_text(encoding="utf-8"))
    for c in cat:
        if "pinyin" not in c:
            c["pinyin"] = to_pinyin(c["name"])
    return cat


def in_catalog(expected: str, cat) -> bool:
    return expected is not None and any(expected in c["name"] for c in cat)


def best(query, cat, mode):
    if mode == "pinyin":
        q = to_pinyin(query)
        choices = {i: c["pinyin"] for i, c in enumerate(cat)}
    else:
        q = query
        choices = {i: c["name"] for i, c in enumerate(cat)}
    idx, score, _ = process.extractOne(q, choices, scorer=fuzz.token_set_ratio)
    # extractOne 回 (choice_value, score, key)；用 key 取回原 row
    key = process.extractOne(q, choices, scorer=fuzz.token_set_ratio)[2]
    return cat[key]["name"], score


def main():
    cat = load_catalog()
    print(f"乾淨目錄 {len(cat)} 首\n")

    # 預算每筆的 best match + score（char / pinyin）
    rows = []
    for q, exp in LABELED:
        cn, cs = best(q, cat, "char")
        pn, ps = best(q, cat, "pinyin")
        rows.append((q, exp, in_catalog(exp, cat), cn, cs, pn, ps))

    # 逐筆（拼音）
    print("=== 逐筆（拼音 scorer）===")
    for q, exp, inc, cn, cs, pn, ps in rows:
        tag = "拒絕" if exp is None else ("在目錄" if inc else "不在目錄")
        ok = "" if exp is None else ("✓" if exp in pn else "✗")
        print(f"  [{tag}] '{q}'  char={cs:.0f} 拼音={ps:.0f} {ok} → {pn[:26]}")

    # 門檻掃描
    print("\n=== 門檻掃描（拼音）：recall = 在目錄正解≥門檻; false = 拒絕案例≥門檻 ===")
    in_cat = [r for r in rows if r[1] and r[2]]      # 有 expected 且在目錄
    rejects = [r for r in rows if r[1] is None]       # 拒絕案例
    print(f"  (in-catalog 樣本 {len(in_cat)}, 拒絕樣本 {len(rejects)})")
    print(f"  {'門檻':>4} | {'拼音 recall':>12} | {'拼音 false':>10} | {'char recall':>11}")
    for th in THRESHOLDS:
        p_rec = sum(1 for r in in_cat if (r[1] in r[5]) and r[6] >= th)
        c_rec = sum(1 for r in in_cat if (r[1] in r[3]) and r[4] >= th)
        p_false = sum(1 for r in rejects if r[6] >= th)
        print(f"  {th:>4} | {p_rec:>4}/{len(in_cat):<3} ({100*p_rec/max(len(in_cat),1):>3.0f}%) | "
              f"{p_false:>4}/{len(rejects):<3} | {c_rec:>4}/{len(in_cat):<3}")


if __name__ == "__main__":
    main()

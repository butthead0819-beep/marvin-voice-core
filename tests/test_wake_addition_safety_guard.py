"""喚醒詞建議的誤喚醒 guard（2026-06-04）。

根因：daily review 的 wake_analysis.suggested_additions 被無條件 append 到
wake_words_override.json，零誤喚醒驗證。早期 Whisper 幻覺把「麻煩/好煩/導航」
當成 馬文 mishear → Gemini 建議 → 自動套用 → 日常詞變喚醒詞狂誤觸發。

guard：建議詞若在 cleaner_gate_drops（被當「非喚醒」丟掉的句子）裡出現 ≥threshold 次
= 日常高頻詞 → 拒收。合法近音詞（馬聞/馬萌…是 馬文 誤聽，非真詞）在 drops 出現 0 次，
不會被誤殺（prod 實測全 0）。
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path


def _import_module():
    mod_name = "scripts.analyze_daily_log"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    base = Path(__file__).parent.parent
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    return importlib.import_module(mod_name)


def _seed_drops(path: Path, raws: list[str]):
    path.write_text(
        "\n".join(json.dumps({"raw": r}, ensure_ascii=False) for r in raws),
        encoding="utf-8",
    )


# ── 1. 高頻日常詞（≥threshold 次出現在 drops）→ 拒收；合法近音（0 次）→ 留 ──
def test_rejects_high_freq_everyday_word_keeps_near_miss(tmp_path):
    mod = _import_module()
    drops = tmp_path / "drops.jsonl"
    _seed_drops(drops, [
        "這個真的很麻煩耶", "好麻煩喔不想用",          # 麻煩 ×2
        "導航到公司", "我開導航", "導航又錯了",          # 導航 ×3
        "今天天氣不錯", "晚點要吃什麼",                  # 無關
    ])
    safe, rejected = mod.filter_unsafe_wake_additions(
        ["麻煩", "導航", "馬萌"], drop_path=drops, threshold=2)
    assert "麻煩" in rejected
    assert "導航" in rejected
    assert "馬萌" in safe          # 合法近音，drops 0 次


# ── 2. 出現次數 < threshold → 不算日常詞，保留 ──
def test_keeps_word_below_threshold(tmp_path):
    mod = _import_module()
    drops = tmp_path / "drops.jsonl"
    _seed_drops(drops, ["偶爾講一次某詞", "其他無關句子"])
    safe, rejected = mod.filter_unsafe_wake_additions(
        ["某詞"], drop_path=drops, threshold=2)
    assert "某詞" in safe          # 只 1 次 < 2
    assert rejected == []


# ── 3. drops 檔讀不到 → 保守放行（不因缺資料拒掉所有新詞） ──
def test_missing_drop_file_keeps_all(tmp_path):
    mod = _import_module()
    safe, rejected = mod.filter_unsafe_wake_additions(
        ["任何詞"], drop_path=tmp_path / "nonexistent.jsonl", threshold=2)
    assert safe == ["任何詞"]
    assert rejected == []

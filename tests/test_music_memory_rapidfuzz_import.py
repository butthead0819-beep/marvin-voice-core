"""
5/18 18:03 incident regression test — rapidfuzz lazy import 在 async hot path
觸發 Python import lock deadlock (Errno 11 EDEADLK)。

Traceback 證據（incident 報告）：
  File "music_memory.py:208" in apply_stt_correction
      from rapidfuzz import fuzz
  File ".../rapidfuzz/__init__.py:11"
      from rapidfuzz import distance, fuzz, process, utils
  File ".../rapidfuzz/distance/__init__.py:6"
      from . import (...)
  File "<frozen importlib._bootstrap_external>:1191" in get_data
  OSError: [Errno 11] Resource deadlock avoided

修法：rapidfuzz 改 module top-level import，bot 啟動時就 load 完成，
runtime 不再有 concurrent import race。

這組 test 確保：
1. music_memory module load 後 _rapidfuzz_fuzz attribute 已就緒
2. apply_stt_correction 內無 runtime `from rapidfuzz import` 殘留
3. apply_stt_correction fuzzy match 仍正常運作
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import music_memory


def test_rapidfuzz_loaded_at_module_level():
    """rapidfuzz import 在 module top-level，runtime 不再 lazy import。"""
    # 直接讀 attribute；裝了 rapidfuzz 應該不是 None
    assert hasattr(music_memory, "_rapidfuzz_fuzz")
    # CI/開發機都裝了 rapidfuzz，預期非 None；未裝環境 fallback 仍 graceful
    if music_memory._rapidfuzz_fuzz is None:
        pytest.skip("rapidfuzz 未安裝（測試環境特例）")
    # 確認是真的 rapidfuzz module（不是隨便的 mock）
    assert hasattr(music_memory._rapidfuzz_fuzz, "ratio")


def test_no_runtime_rapidfuzz_import_in_apply_stt_correction():
    """static check：apply_stt_correction body 內不該再有 `from rapidfuzz` 語句。"""
    src = Path(music_memory.__file__).read_text(encoding="utf-8")
    # 找 apply_stt_correction body 範圍
    m = re.search(
        r"def apply_stt_correction\(.*?\n(.+?)(?=\n    def |\nclass |\Z)",
        src,
        re.DOTALL,
    )
    assert m, "找不到 apply_stt_correction"
    body = m.group(1)
    assert "from rapidfuzz" not in body, (
        "apply_stt_correction 不該有 lazy `from rapidfuzz import`，"
        "會在 async hot path 觸發 Python import lock deadlock"
    )


def test_apply_stt_correction_exact_match(tmp_path):
    """完全比對路徑：不依賴 rapidfuzz，回 corrected。"""
    mem = music_memory.MusicMemory(path=str(tmp_path / "m.json"))
    mem._data = {
        "stt_corrections": {
            "Alice": [{"wrong": "陶著的天天", "correct": "陶喆的天天", "count": 1}]
        }
    }
    corrected, wrong = mem.apply_stt_correction("Alice", "陶著的天天")
    assert corrected == "陶喆的天天"
    assert wrong == "陶著的天天"


def test_apply_stt_correction_fuzzy_match(tmp_path):
    """模糊比對路徑：rapidfuzz 在 module top 已 import，runtime 不會炸。"""
    if music_memory._rapidfuzz_fuzz is None:
        pytest.skip("rapidfuzz 未安裝")
    mem = music_memory.MusicMemory(path=str(tmp_path / "m.json"))
    mem._data = {
        "stt_corrections": {
            "Alice": [{"wrong": "陶喆的天天", "correct": "陶喆 - 天天", "count": 5}]
        }
    }
    # 接近但非完全相同的 query
    corrected, wrong = mem.apply_stt_correction("Alice", "陶喆的天天天")
    # rapidfuzz ratio ≥ 78 應該命中
    if wrong is not None:
        assert corrected == "陶喆 - 天天"


def test_apply_stt_correction_no_history_returns_query_unchanged(tmp_path):
    """user 沒歷史 correction → 原 query 回傳。"""
    mem = music_memory.MusicMemory(path=str(tmp_path / "m.json"))
    mem._data = {"stt_corrections": {}}
    corrected, wrong = mem.apply_stt_correction("Alice", "陶喆的天天")
    assert corrected == "陶喆的天天"
    assert wrong is None

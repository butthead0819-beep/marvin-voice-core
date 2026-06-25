"""conftest 通用 records 防污染攔截的回歸測試。

2026-06-25 根除：原本 conftest 只逐 writer patch（GapLogger / speak_outcome / …），
proactive_usage 沒被任何條覆蓋 → 測試（trigger_proactive_topic）灌了 397 筆 Alice/Bob
假表演進 prod records/proactive_usage.jsonl，毒到 daily_review runaway。

改成攔 open() / Path.open() 邊界後，任何 writer（現在或未來）寫 prod records/ 都不可能
再污染。這些測試守住那個性質——任一條紅 = 通用攔截破了 = 污染風險回來了。
"""
from pathlib import Path

import pytest


def test_builtins_open_write_to_records_not_polluting_prod():
    """裸 open('records/...','a') 寫入被導到 tmp，prod 檔狀態不變。"""
    prod = Path("records/_conftest_probe_builtins.jsonl")
    before = prod.exists()
    with open("records/_conftest_probe_builtins.jsonl", "a", encoding="utf-8") as f:
        f.write('{"pollution": true}\n')
    assert prod.exists() == before, "裸 open 寫入污染了 prod records/"


def test_path_open_write_to_records_not_polluting_prod():
    """Path('records/...').open('w') 也被導到 tmp。"""
    prod = Path("records/_conftest_probe_pathopen.jsonl")
    before = prod.exists()
    with Path("records/_conftest_probe_pathopen.jsonl").open("w", encoding="utf-8") as f:
        f.write("x\n")
    assert prod.exists() == before, "Path.open 寫入污染了 prod records/"


def test_records_subdir_write_succeeds_in_tmp():
    """寫到 records/子目錄/ 能成功（攔截器建好 tmp 父目錄），且不碰 prod。"""
    prod = Path("records/_probe_subdir/x.log")
    before = prod.exists()
    with open("records/_probe_subdir/x.log", "w", encoding="utf-8") as f:
        f.write("ok\n")
    assert prod.exists() == before


def test_read_mode_not_redirected():
    """read 模式不導向——測試讀 prod fixture 的路徑語意不變。"""
    with pytest.raises(FileNotFoundError):
        open("records/_definitely_absent_conftest_probe.jsonl", "r")

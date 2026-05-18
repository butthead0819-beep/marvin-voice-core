"""Regression: MemoryManager._save_data() AttributeError dead references.

舊版 MemoryManager (JSON-backed) 有 _save_data()；重構成 SQLite 後改為
per-mutation auto-commit + flush() no-op。但兩處呼叫點沒同步：

  - cogs/voice_controller.py self_restart() 噴 AttributeError → /marvin_reboot 失效
  - gemini_router_content.py 記憶清洗 silently broken（被外層 except 吞掉）

這組測試保證未來不再回頭引用不存在的 _save_data()，以及 MemoryManager 仍提供
flush() 作為公開儲存 API。
"""
from pathlib import Path

import pytest

from suki_memory import MemoryManager


ROOT = Path(__file__).resolve().parent.parent


def test_memory_manager_has_no_save_data_attribute(tmp_path):
    mem = MemoryManager(
        db_path=str(tmp_path / "t.db"),
        json_compat_path=str(tmp_path / "t.json"),
    )
    assert not hasattr(mem, "_save_data"), (
        "MemoryManager 不應再提供 _save_data()。改用 flush() 或 _save_player()。"
    )


def test_memory_manager_exposes_flush(tmp_path):
    mem = MemoryManager(
        db_path=str(tmp_path / "t.db"),
        json_compat_path=str(tmp_path / "t.json"),
    )
    mem.flush()  # 不能噴錯


@pytest.mark.parametrize("rel_path", [
    "cogs/voice_controller.py",
    "gemini_router_content.py",
])
def test_no_dead_save_data_call_sites(rel_path):
    src = (ROOT / rel_path).read_text(encoding="utf-8")
    assert "_save_data(" not in src, (
        f"{rel_path} 仍引用 MemoryManager._save_data()，這個 method 已被刪除，"
        f"執行到該行會噴 AttributeError。"
    )

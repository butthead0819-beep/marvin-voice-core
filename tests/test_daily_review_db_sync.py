"""
suki DB/JSON 同步斷裂修復（TODO P1）。

斷裂：daily review（analyze_daily_log.py）只寫 suki_memory.json，但 bot 從 marvin.db
讀且只在 db 空時 migrate → daily 的 player 分析（likes/impression/relationship）永遠進
不了 runtime；bot _export_json 還會用 db 覆蓋抹掉 daily 寫的 json player 區段。

修法（Jack 2026-05-22 拍板選項 1）：daily review 把合併後的 player 改用 MemoryManager
寫進 db（權威來源）；meta（marvin_performance / proactive_topics 等）繼續寫 json。

這裡測新抽出的 persist_players_to_db()：
  1. 把指定 player 寫進 SQLite（fresh MemoryManager 讀得到）
  2. 只寫 names 指定的 player，未列名者不動（降低與 bot 並發寫入的衝突面）
  3. 寫回後保留 json 既有 meta key
  4. names 列了但 players 沒有 → 跳過不崩潰，回傳實際寫入筆數
"""
from __future__ import annotations

import importlib
import json
import sqlite3
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


def _read_player_from_db(db_path: str, username: str) -> dict | None:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT data FROM players WHERE username = ?", (username,)
        ).fetchone()
    return json.loads(row[0]) if row else None


# ── 1. 寫進 SQLite（落盤，不靠 cache）─────────────────────────────────────────

def test_persist_players_writes_merged_player_to_sqlite(tmp_path):
    mod = _import_module()
    db = str(tmp_path / "t.db")
    jpath = str(tmp_path / "t.json")

    merged = {
        "大肚": {
            "personal_info": {},
            "likes": ["與友共飲", "駕駛油車"],
            "dislikes": [],
            "taboos": [],
            "suki_impression": "daily 分析寫入的長印象",
        }
    }
    written = mod.persist_players_to_db(merged, ["大肚"], db_path=db, json_path=jpath)

    assert written == 1
    got = _read_player_from_db(db, "大肚")
    assert got is not None, "daily 合併結果必須落進 marvin.db，否則 bot 永遠讀不到"
    assert "與友共飲" in got["likes"]
    assert got["suki_impression"] == "daily 分析寫入的長印象"


# ── 2. 只寫 names 指定者，未列名玩家不被覆蓋 ─────────────────────────────────

def test_persist_players_only_writes_named(tmp_path):
    mod = _import_module()
    db = str(tmp_path / "t.db")
    jpath = str(tmp_path / "t.json")

    from suki_memory import MemoryManager
    # 預置一位「今日未出現」玩家，模擬 bot runtime 已有的 db 內容
    seed = MemoryManager(db_path=db, json_compat_path=jpath)
    seed.replace_player_memory("showay", {"personal_info": {}, "likes": ["原本的"]})
    del seed

    merged = {
        "大肚": {"personal_info": {}, "likes": ["新的"]},
        "showay": {"personal_info": {}, "likes": ["不該被寫入的覆蓋值"]},
    }
    # 只把「大肚」列入 names → showay 不該被動到
    written = mod.persist_players_to_db(merged, ["大肚"], db_path=db, json_path=jpath)

    assert written == 1
    assert "新的" in _read_player_from_db(db, "大肚")["likes"]
    assert _read_player_from_db(db, "showay")["likes"] == ["原本的"], (
        "未列名玩家不該被覆蓋，避免與 bot 並發寫入衝突"
    )


# ── 3. 寫回後保留 json 既有 meta（marvin_performance 等不被 nuke）─────────────

def test_persist_players_preserves_json_meta(tmp_path):
    mod = _import_module()
    db = str(tmp_path / "t.db")
    jpath = str(tmp_path / "t.json")

    from suki_memory import MemoryManager
    seed = MemoryManager(db_path=db, json_compat_path=jpath)
    seed.replace_player_memory("大肚", {"personal_info": {}, "likes": ["舊"]})
    del seed

    # daily review 已先把 meta 寫進 json（模擬 line 1383 的寫出）
    existing = json.loads(Path(jpath).read_text(encoding="utf-8"))
    existing["marvin_performance"] = {"score": 7.5, "trend": "改善"}
    existing["proactive_topics"] = [{"title": "話題A"}]
    Path(jpath).write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")

    mod.persist_players_to_db({"大肚": {"personal_info": {}, "likes": ["新"]}},
                              ["大肚"], db_path=db, json_path=jpath)

    after = json.loads(Path(jpath).read_text(encoding="utf-8"))
    assert after["marvin_performance"]["score"] == 7.5, "meta 不可被 player 寫回抹掉"
    assert after["proactive_topics"] == [{"title": "話題A"}]
    assert "新" in after["players"]["大肚"]["likes"], "json player 區段應同步成新值"


# ── 4. names 列了但 players 缺 → 跳過不崩潰 ──────────────────────────────────

def test_persist_players_skips_missing_name(tmp_path):
    mod = _import_module()
    db = str(tmp_path / "t.db")
    jpath = str(tmp_path / "t.json")

    written = mod.persist_players_to_db(
        {"大肚": {"personal_info": {}}}, ["大肚", "不存在的人"],
        db_path=db, json_path=jpath,
    )

    assert written == 1
    assert _read_player_from_db(db, "不存在的人") is None

"""TDD — StateStore：共用 JSON-backed KV store（atomic write + fail-open read + 鎖）。"""
from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

from state_store import StateStore


def test_save_then_load_roundtrip(tmp_path):
    store = StateStore(str(tmp_path / "data.json"))
    store.save({"a": 1, "b": "中文值"})
    assert store.load() == {"a": 1, "b": "中文值"}


def test_load_missing_file_returns_empty_dict(tmp_path):
    store = StateStore(str(tmp_path / "does_not_exist.json"))
    assert store.load() == {}


def test_load_missing_file_returns_custom_default(tmp_path):
    store = StateStore(str(tmp_path / "does_not_exist.json"), default={"x": []})
    assert store.load() == {"x": []}


def test_load_corrupted_json_fails_open(tmp_path):
    path = tmp_path / "corrupt.json"
    path.write_text("{not valid json!!", encoding="utf-8")
    store = StateStore(str(path))
    # 壞檔不能讓呼叫端炸掉，回傳空 dict（fail-open）
    assert store.load() == {}


def test_load_corrupted_json_does_not_raise(tmp_path):
    path = tmp_path / "corrupt.json"
    path.write_text("]] this is garbage [[", encoding="utf-8")
    store = StateStore(str(path))
    try:
        result = store.load()
    except Exception as e:  # noqa: BLE001 - 這裡就是要確認完全不拋例外
        pytest.fail(f"load() 不該拋例外，但拋了 {e!r}")
    assert result == {}


def test_update_reads_modifies_writes_under_lock(tmp_path):
    store = StateStore(str(tmp_path / "data.json"))
    store.save({"count": 1})

    result = store.update(lambda d: {**d, "count": d["count"] + 1})

    assert result == {"count": 2}
    assert store.load() == {"count": 2}


def test_update_on_missing_file_starts_from_default(tmp_path):
    store = StateStore(str(tmp_path / "data.json"), default={})

    def bump(d):
        d["hits"] = d.get("hits", 0) + 1
        return d

    store.update(bump)
    store.update(bump)

    assert store.load() == {"hits": 2}


def test_concurrent_updates_do_not_overwrite_each_other(tmp_path):
    """模擬併發 read-modify-write：兩次 update() 依序執行，結果必須是兩次累加，
    不能因為互蓋只剩一次的效果（這正是 music_memory.json 之前踩過的 race）。"""
    path = str(tmp_path / "data.json")
    store_a = StateStore(path)
    store_b = StateStore(path)
    store_a.save({"n": 0})

    store_a.update(lambda d: {**d, "n": d["n"] + 1})
    store_b.update(lambda d: {**d, "n": d["n"] + 1})

    assert store_a.load() == {"n": 2}


def test_save_writes_via_tmp_file_then_atomic_replace(tmp_path):
    """驗證寫入機制：save() 要先寫暫存檔，再用 os.replace() 換檔，
    確保讀者不會讀到寫一半的內容。用 mock 觀察呼叫順序，不用真的中斷 process。"""
    path = tmp_path / "data.json"
    store = StateStore(str(path))

    real_replace = os.replace
    calls = []

    def spy_replace(src, dst):
        calls.append((src, dst))
        # 换檔前，暫存檔必須已經存在且內容完整（json 可解析）
        assert os.path.exists(src)
        with open(src, encoding="utf-8") as f:
            assert json.load(f) == {"k": "v"}
        return real_replace(src, dst)

    with patch("os.replace", side_effect=spy_replace) as mock_replace:
        store.save({"k": "v"})

    assert mock_replace.called
    tmp_src, dst = calls[0]
    assert tmp_src != str(path)  # 確實是先寫到不同的暫存路徑
    assert dst == str(path)
    assert store.load() == {"k": "v"}


def test_original_file_untouched_if_replace_never_happens(tmp_path):
    """模擬寫入中斷：save() 過程中 os.replace 拋例外（模擬中斷），
    原始檔案內容不應該被破壞（fail-open：吞例外，不留半寫的檔案）。"""
    path = tmp_path / "data.json"
    store = StateStore(str(path))
    store.save({"orig": True})

    with patch("os.replace", side_effect=OSError("simulated interruption")):
        store.save({"orig": False, "corrupted": "should not land"})

    # save() 內部 fail-open 吞掉例外，不拋出
    assert store.load() == {"orig": True}


def test_lock_file_created_alongside_data_file(tmp_path):
    path = tmp_path / "data.json"
    store = StateStore(str(path))
    store.update(lambda d: {**d, "x": 1})
    assert (tmp_path / "data.json.lock").exists()

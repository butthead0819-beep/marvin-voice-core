"""manzai_variety — 漫才變化性：ボケ 手法輪替 + 最近用過句子的 no-repeat ring buffer。

治「每次漫才都是『我只是一堆代碼 + X 也是虛無』＋『記得喝水』」的重複感
（2026-09-22 實測五次全中同一模板）。
"""
from __future__ import annotations

import json

import pytest

from manzai_variety import (
    BOKE_MODES,
    RING_SIZE,
    ManzaiVarietyStore,
    boke_block,
    build_avoid_block,
    pick_boke_mode,
    recent_lines,
    record_lines,
)


@pytest.fixture
def store(tmp_path):
    return ManzaiVarietyStore(str(tmp_path / "manzai_variety.json"))


# ── 1. ボケ 手法輪替 ─────────────────────────────────────────────────────────

def test_pick_boke_mode_not_same_twice_in_a_row(store):
    """連續兩次不會選到同一個 mode——重複感的主因就是永遠落在同一種手法。"""
    first = pick_boke_mode(store)
    second = pick_boke_mode(store)
    assert first in BOKE_MODES
    assert second in BOKE_MODES
    assert second != first


def test_pick_boke_mode_covers_all_modes_in_one_cycle(store):
    """一輪要走完全部手法——只排除「上一次」會在前兩種之間乒乓，其餘永遠輪不到
    （2026-09-22 實測：五次生成只出現 existential / literal 兩種）。"""
    picked = [pick_boke_mode(store) for _ in range(len(BOKE_MODES))]
    assert set(picked) == set(BOKE_MODES)


def test_pick_boke_mode_persists_across_store_instances(tmp_path):
    """重啟（重建 store）仍記得上次選了什麼，下一次不會重複它。"""
    path = str(tmp_path / "manzai_variety.json")
    first = pick_boke_mode(ManzaiVarietyStore(path))
    second = pick_boke_mode(ManzaiVarietyStore(path))
    assert second != first


# ── 2. ring buffer ──────────────────────────────────────────────────────────

def test_record_lines_keeps_only_recent_ring_size(store):
    """超過 RING_SIZE 只留最近幾筆，順序新→舊。"""
    for i in range(RING_SIZE + 3):
        record_lines(store, [{"voice": "marvin", "text": f"第{i}句"}])
    lines = recent_lines(store)
    assert len(lines) == RING_SIZE
    newest = RING_SIZE + 2
    assert lines[0] == f"第{newest}句"
    assert lines[-1] == f"第{newest - RING_SIZE + 1}句"
    assert "第0句" not in lines


def test_record_lines_truncates_on_write_not_just_on_read(store, tmp_path):
    """寫入端就要截斷——只靠 recent_lines(n=) 讀端截會讓 JSON 無限長大。"""
    for i in range(RING_SIZE * 3):
        record_lines(store, [{"voice": "marvin", "text": f"第{i}句"}])
    stored = json.loads((tmp_path / "manzai_variety.json").read_text(encoding="utf-8"))
    assert len(stored["_recent_lines"]) == RING_SIZE
    assert len(recent_lines(store, n=999)) == RING_SIZE


def test_record_lines_records_every_segment_text(store):
    record_lines(store, [
        {"voice": "marvin", "text": "馬文這句"},
        {"voice": "marmo", "text": "馬末這句"},
    ])
    assert recent_lines(store) == ["馬末這句", "馬文這句"]


def test_recent_lines_respects_n(store):
    for i in range(RING_SIZE):
        record_lines(store, [{"voice": "marvin", "text": f"第{i}句"}])
    assert len(recent_lines(store, n=2)) == 2


# ── 3. fail-open ────────────────────────────────────────────────────────────

def test_corrupt_file_is_fail_open(tmp_path):
    """壞掉的 JSON 當空資料，不拋例外擋住漫才生成。"""
    path = tmp_path / "manzai_variety.json"
    path.write_text("{ this is not json", encoding="utf-8")
    s = ManzaiVarietyStore(str(path))
    assert recent_lines(s) == []
    assert pick_boke_mode(s) in BOKE_MODES
    record_lines(s, [{"voice": "marvin", "text": "x"}])


def test_unwritable_path_does_not_raise(tmp_path):
    s = ManzaiVarietyStore("/proc/nonexistent-dir/manzai.json")
    record_lines(s, [{"voice": "marvin", "text": "x"}])
    assert pick_boke_mode(s) in BOKE_MODES


# ── 4. boke block 互斥 ──────────────────────────────────────────────────────

def test_boke_blocks_are_mutually_exclusive():
    """任一 mode 的 block 不得含其他 mode 的特徵詞——否則 prompt 又把手法糊在一起。"""
    markers = {
        "existential": ("虛無", "消散"),
        "literal": ("字面",),
        "hyper_precise": ("小數點",),
        "epic": ("史詩",),
        "self_deprecate": ("行星",),
    }
    for mode, own in markers.items():
        block = boke_block(mode)
        for word in own:
            assert word in block, f"{mode} 的 block 應含自己的特徵詞 {word}"
        for other, other_words in markers.items():
            if other == mode:
                continue
            for word in other_words:
                assert word not in block, f"{mode} 的 block 不該含 {other} 的特徵詞 {word}"


def test_boke_block_unknown_mode_falls_back():
    assert boke_block("no-such-mode") == boke_block("existential")


def test_all_modes_have_block():
    for mode in BOKE_MODES:
        assert boke_block(mode).strip()


# ── 5. avoid block ──────────────────────────────────────────────────────────

def test_build_avoid_block_empty_when_no_recent():
    assert build_avoid_block([]) == ""
    assert build_avoid_block(None) == ""


def test_build_avoid_block_lists_lines():
    block = build_avoid_block(["我只是一堆代碼", "記得喝水"])
    assert "我只是一堆代碼" in block
    assert "記得喝水" in block
    assert "禁止" in block or "嚴禁" in block


def test_store_file_is_valid_json_after_writes(store, tmp_path):
    record_lines(store, [{"voice": "marvin", "text": "一"}])
    pick_boke_mode(store)
    data = json.loads((tmp_path / "manzai_variety.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)

"""TDD: 防線② 跨人記憶隔離 — Tier 4 社交死的機制性防線。

失效模式：A 的 per-person 記憶（禁忌話題/personal_info）流進 B 觸發的
prompt、被 Marvin 對全房說出來——發生一次就結案的信任事故。

2026-07-02 審計結論：隔離機制大半已存在（vector where 過濾、
target_speakers=[speaker]+online、shareable flag），但**零測試在守**。
本檔把不變量升格為 invariant 測試 + 有名字的 helper：

  I1  記憶注入名單 = 當前 speaker + 在場者（speaker_isolation.present_speakers）
  I2  prompt 注入只含名單內的人的記憶（名單外的人 hooks 不得出現）
  I3  vector 搜尋不得回其他 speaker / 其他 guild 的片段
  I4  非 shareable callback 不得從 shareable 出口流出（補既有測試的反向斷言）
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from speaker_isolation import present_speakers


# ── I1: 注入名單建構 ─────────────────────────────────────────────────────────

def test_present_speakers_is_speaker_plus_online():
    assert present_speakers("阿明", ["狗與露", "大肚"]) == ["阿明", "狗與露", "大肚"]


def test_present_speakers_dedups_current_speaker():
    assert present_speakers("阿明", ["阿明", "大肚"]) == ["阿明", "大肚"]


def test_present_speakers_no_online_returns_speaker_only():
    assert present_speakers("阿明", None) == ["阿明"]
    assert present_speakers("阿明", []) == ["阿明"]


def test_present_speakers_filters_empty_names():
    assert present_speakers("阿明", ["", None, "大肚"]) == ["阿明", "大肚"]


# ── I2: prompt 注入不含名單外的人 ─────────────────────────────────────────────

def _mem_manager(records: dict) -> MagicMock:
    mm = MagicMock()
    mm.get_player_memory.side_effect = lambda name: records.get(name, {})
    return mm


@pytest.mark.parametrize("layer", ["dere_persona", "fast_awakening"])
def test_prompt_memory_injection_excludes_absent_member(layer):
    """在場=阿明+狗與露；大肚不在場 → 大肚的記憶（含禁忌）不得進 prompt。"""
    from marvin_prompts import PromptManager
    records = {
        "阿明": {"personal_info": {"food": "肉圓"}},
        "狗與露": {"likes": ["周杰倫"]},
        "大肚": {"personal_info": {"salary": "44K"}, "taboos": ["前女友"]},
    }
    prompt = PromptManager().get_instruction(
        layer, vision_enabled=False,
        speaker=present_speakers("阿明", ["狗與露"]),
        memory_manager=_mem_manager(records),
    )
    assert "肉圓" in prompt
    assert "周杰倫" in prompt
    # 不在場的大肚：任何記憶欄位都不得出現
    assert "44K" not in prompt
    assert "前女友" not in prompt
    assert "大肚" not in prompt


def test_prompt_single_speaker_excludes_everyone_else():
    from marvin_prompts import PromptManager
    records = {
        "阿明": {"personal_info": {"food": "肉圓"}},
        "大肚": {"taboos": ["前女友"]},
    }
    prompt = PromptManager().get_instruction(
        "dere_persona", vision_enabled=False,
        speaker="阿明", memory_manager=_mem_manager(records),
    )
    assert "肉圓" in prompt
    assert "前女友" not in prompt


# ── I3: vector 搜尋跨人隔離（真 chroma，非 mock）─────────────────────────────

def test_vector_search_never_returns_other_speaker_docs(tmp_path):
    from vector_store import VectorStore
    vs = VectorStore(persist_dir=str(tmp_path / "chroma"))
    vs.upsert("阿明", 123, "阿明超愛吃肉圓配大杯珍奶", doc_id="a1")
    vs.upsert("大肚", 123, "大肚的薪水是44K不想被提起", doc_id="b1")

    hits = vs.search("阿明", 123, "薪水 44K 肉圓", top_k=5)

    assert all("44K" not in h for h in hits)


def test_vector_search_never_crosses_guild(tmp_path):
    from vector_store import VectorStore
    vs = VectorStore(persist_dir=str(tmp_path / "chroma"))
    vs.upsert("阿明", 123, "guild123 的秘密計畫", doc_id="g1")
    hits = vs.search("阿明", 999, "秘密計畫", top_k=5)
    assert hits == []


# ── I4: shareable gate 反向斷言 ──────────────────────────────────────────────

def test_non_shareable_callback_never_leaves_via_shareable_exit(tmp_path):
    from suki_memory import MemoryManager
    mem = MemoryManager(db_path=str(tmp_path / "m.db"))
    mem.enqueue_callback("阿明", "私密：上次喝掛吐在超商門口", shareable=False)

    assert mem.peek_shareable_callback("阿明") is None
    assert all("喝掛" not in str(c) for c in mem.peek_all_shareable_callbacks("阿明"))

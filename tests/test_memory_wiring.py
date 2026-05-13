"""
tests/test_memory_wiring.py
測試兩條接線：
  Wire 1: transcript save → vector upsert（save 後向量庫應可搜尋到）
  Wire 2: 已在 test_context_injector.py::test_enrich_triggers_background_compression 覆蓋
"""
import pytest
from transcript_store import TranscriptStore
from vector_store import VectorStore


def test_save_then_upsert_makes_searchable(tmp_path):
    """Wire 1：存逐字稿後立刻 upsert，向量搜尋應能找到該內容"""
    ts = TranscriptStore(db_path=":memory:")
    vs = VectorStore(persist_dir=str(tmp_path))

    speaker, guild_id, text, timestamp = "alice", 1, "我最近在考慮換工作", 1000.0
    ts.save(speaker, guild_id, text, timestamp)
    doc_id = f"{speaker}_{guild_id}_{int(timestamp * 1000)}"
    vs.upsert(speaker, guild_id, text, doc_id)

    results = vs.search(speaker, guild_id, "工作規劃", top_k=1)
    assert len(results) == 1
    assert "換工作" in results[0]


def test_multiple_saves_all_searchable(tmp_path):
    """多筆逐字稿全部 upsert 後都能被搜尋到"""
    ts = TranscriptStore(db_path=":memory:")
    vs = VectorStore(persist_dir=str(tmp_path))

    utterances = [
        ("alice", 1, "今天去打羽球", 1000.0),
        ("alice", 1, "最近在學 Python", 2000.0),
        ("alice", 1, "想去旅行", 3000.0),
    ]
    for speaker, guild_id, text, ts_val in utterances:
        ts.save(speaker, guild_id, text, ts_val)
        vs.upsert(speaker, guild_id, text, f"{speaker}_{guild_id}_{int(ts_val * 1000)}")

    results = vs.search("alice", 1, "運動", top_k=3)
    assert any("羽球" in r for r in results)

import pytest
from vector_store import VectorStore


def test_upsert_and_search_returns_relevant(tmp_path):
    store = VectorStore(persist_dir=str(tmp_path))
    store.upsert("alice", 1, "我最近在學 Python 程式設計", "doc1")
    results = store.search("alice", 1, "Python 學習", top_k=1)
    assert len(results) == 1
    assert "Python" in results[0]


def test_search_filters_by_speaker(tmp_path):
    store = VectorStore(persist_dir=str(tmp_path))
    store.upsert("alice", 1, "Alice 喜歡打羽球", "doc_alice")
    store.upsert("bob", 1, "Bob 喜歡踢足球", "doc_bob")
    results = store.search("alice", 1, "運動", top_k=5)
    assert all("Alice" in r or "alice" in r.lower() or "羽球" in r for r in results)
    assert not any("Bob" in r for r in results)


def test_search_empty_collection_returns_empty(tmp_path):
    store = VectorStore(persist_dir=str(tmp_path))
    results = store.search("nobody", 1, "任何問題", top_k=3)
    assert results == []


def test_delete_speaker_removes_documents(tmp_path):
    store = VectorStore(persist_dir=str(tmp_path))
    store.upsert("alice", 1, "Alice 的資料應該被刪除", "doc1")
    store.delete_speaker("alice", 1)
    results = store.search("alice", 1, "資料", top_k=3)
    assert results == []

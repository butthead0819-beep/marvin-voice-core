"""
VectorStore 三個新方法的 TDD 測試：get_all / delete / update。

設計目的：companion bridge 直接 import VectorStore，
透過 ChromaDB 原生語意操作記憶；不額外加抽象層。
"""

from vector_store import VectorStore


def test_get_all_returns_speaker_documents(tmp_path):
    store = VectorStore(persist_dir=str(tmp_path))
    store.upsert("alice", 1, "事實一", "doc1")
    store.upsert("alice", 1, "事實二", "doc2")
    store.upsert("alice", 1, "事實三", "doc3")

    results = store.get_all("alice", 1)
    ids = {r["id"] for r in results}
    assert ids == {"doc1", "doc2", "doc3"}
    # 每筆都應該帶有 document 與 metadata
    for r in results:
        assert "document" in r
        assert "metadata" in r
        assert r["metadata"]["speaker"] == "alice"


def test_get_all_respects_limit(tmp_path):
    store = VectorStore(persist_dir=str(tmp_path))
    for i in range(10):
        store.upsert("alice", 1, f"事實 {i}", f"doc{i}")

    results = store.get_all("alice", 1, limit=5)
    assert len(results) == 5


def test_get_all_filters_by_guild(tmp_path):
    store = VectorStore(persist_dir=str(tmp_path))
    store.upsert("alice", 1, "公會 1 的事實", "doc_g1")
    store.upsert("alice", 2, "公會 2 的事實", "doc_g2")

    results = store.get_all("alice", 1)
    assert len(results) == 1
    assert results[0]["id"] == "doc_g1"
    assert results[0]["metadata"]["guild_id"] == "1"


def test_get_all_returns_empty_for_unknown_speaker(tmp_path):
    store = VectorStore(persist_dir=str(tmp_path))
    store.upsert("alice", 1, "事實", "doc1")

    results = store.get_all("ghost", 1)
    assert results == []


def test_delete_removes_single_doc(tmp_path):
    store = VectorStore(persist_dir=str(tmp_path))
    store.upsert("alice", 1, "保留", "keep")
    store.upsert("alice", 1, "刪除我", "drop")

    store.delete("drop")

    remaining_ids = {r["id"] for r in store.get_all("alice", 1)}
    assert remaining_ids == {"keep"}


def test_delete_missing_id_is_noop(tmp_path):
    store = VectorStore(persist_dir=str(tmp_path))
    store.upsert("alice", 1, "事實", "doc1")

    # 不應該丟例外
    store.delete("does_not_exist")

    remaining_ids = {r["id"] for r in store.get_all("alice", 1)}
    assert remaining_ids == {"doc1"}


def test_update_sets_metadata(tmp_path):
    store = VectorStore(persist_dir=str(tmp_path))
    store.upsert("alice", 1, "可疑的記憶", "doc1")

    store.update("doc1", {"uncertain": True})

    results = store.get_all("alice", 1)
    assert results[0]["metadata"].get("uncertain") is True


def test_update_preserves_existing_metadata(tmp_path):
    store = VectorStore(persist_dir=str(tmp_path))
    store.upsert("alice", 1, "原始記憶", "doc1")

    store.update("doc1", {"uncertain": True})

    results = store.get_all("alice", 1)
    md = results[0]["metadata"]
    assert md["speaker"] == "alice"
    assert md["guild_id"] == "1"
    assert md.get("uncertain") is True

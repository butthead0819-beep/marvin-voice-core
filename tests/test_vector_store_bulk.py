"""
VectorStore.get_profiles_bulk() 的 TDD 測試。

設計目的：TopicGenerator 一次查詢多個頻道成員的 Living Profile，
回傳 list[str]（每個元素是 document 文字），跳過無 profile 的成員。
"""

from vector_store import VectorStore


def test_get_profiles_bulk_all_have_profiles(tmp_path):
    """3 個 speaker 都有 profile → 回傳 3 個字串。"""
    store = VectorStore(persist_dir=str(tmp_path))
    store.upsert("alice", 1, "Alice 喜歡打羽球", "doc_alice")
    store.upsert("bob", 1, "Bob 喜歡踢足球", "doc_bob")
    store.upsert("carol", 1, "Carol 喜歡游泳", "doc_carol")

    results = store.get_profiles_bulk(["alice", "bob", "carol"], "1")
    assert len(results) == 3
    assert all(isinstance(r, str) for r in results)
    texts = " ".join(results)
    assert "Alice" in texts or "羽球" in texts
    assert "Bob" in texts or "足球" in texts
    assert "Carol" in texts or "游泳" in texts


def test_get_profiles_bulk_one_missing(tmp_path):
    """3 個 speaker，其中 1 個無 profile → 回傳 2 個字串，無 None 無空字串。"""
    store = VectorStore(persist_dir=str(tmp_path))
    store.upsert("alice", 1, "Alice 的資料", "doc_alice")
    store.upsert("carol", 1, "Carol 的資料", "doc_carol")
    # bob 沒有插入任何資料

    results = store.get_profiles_bulk(["alice", "bob", "carol"], "1")
    assert len(results) == 2
    assert all(isinstance(r, str) for r in results)
    assert all(r for r in results)  # 無空字串


def test_get_profiles_bulk_empty_speaker_ids(tmp_path):
    """空的 speaker_ids → 回傳 []。"""
    store = VectorStore(persist_dir=str(tmp_path))
    store.upsert("alice", 1, "Alice 的資料", "doc_alice")

    results = store.get_profiles_bulk([], "1")
    assert results == []


def test_get_profiles_bulk_all_missing(tmp_path):
    """所有 speaker 都無 profile → 回傳 []。"""
    store = VectorStore(persist_dir=str(tmp_path))

    results = store.get_profiles_bulk(["ghost1", "ghost2"], "1")
    assert results == []


def test_get_profiles_bulk_single_speaker_with_profile(tmp_path):
    """單一 speaker 有 profile → 回傳 [一個字串]。"""
    store = VectorStore(persist_dir=str(tmp_path))
    store.upsert("alice", 1, "Alice 是個工程師", "doc_alice")

    results = store.get_profiles_bulk(["alice"], "1")
    assert len(results) == 1
    assert isinstance(results[0], str)
    assert "Alice" in results[0]


def test_get_profiles_bulk_filters_by_guild(tmp_path):
    """同一 speaker 在不同 guild 的 profile 不會混入。"""
    store = VectorStore(persist_dir=str(tmp_path))
    store.upsert("alice", 1, "公會一的 Alice", "doc_g1")
    store.upsert("alice", 2, "公會二的 Alice", "doc_g2")

    results = store.get_profiles_bulk(["alice"], "1")
    assert len(results) == 1
    assert "公會一" in results[0]
    assert "公會二" not in results[0]


def test_get_profiles_bulk_returns_strings_not_dicts(tmp_path):
    """確認回傳型別是 list[str]，每個元素是 document 文字而非 dict。"""
    store = VectorStore(persist_dir=str(tmp_path))
    store.upsert("alice", 1, "Alice 的個人資料", "doc_alice")

    results = store.get_profiles_bulk(["alice"], "1")
    assert len(results) == 1
    assert isinstance(results[0], str)
    # 不是 dict，沒有 "id" 或 "metadata" 鍵
    assert not isinstance(results[0], dict)

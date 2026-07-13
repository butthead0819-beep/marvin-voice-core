"""沙盒下 VectorStore（.chroma_db）寫入 no-op、讀取繼承正本。"""
import pytest

import memory_sandbox

chromadb = pytest.importorskip("chromadb")


@pytest.fixture(autouse=True)
def _clean():
    memory_sandbox.deactivate()
    yield
    memory_sandbox.deactivate()


def test_vector_store_sandbox_write_noop(tmp_path):
    from vector_store import VectorStore
    persist = str(tmp_path / "chroma")
    vs = VectorStore(persist_dir=persist)
    vs.upsert("狗與露", 1, "seed memory", "d1")
    assert vs._col.count() == 1

    memory_sandbox.activate()
    sb = VectorStore(persist_dir=persist)
    # 讀繼承正本
    assert sb.get_all("狗與露", 1)[0]["document"] == "seed memory"
    # 寫入 no-op
    sb.upsert("狗與露", 1, "ghost", "d2")
    sb.delete("d1")
    sb.update("d1", {"x": "y"})
    assert sb._col.count() == 1  # 正本零污染

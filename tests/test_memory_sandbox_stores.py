"""沙盒下各 store 的寫入 no-op + 讀取繼承正本 + 正本零污染。

驗證的不變式（對每個 store）：
  1. 沙盒啟用時呼叫寫入方法 → 不拋錯（graceful no-op）
  2. 正本 DB 的資料列數不變（沒被污染）
  3. 讀取方法仍讀得到正本既有資料（繼承正本＝Marvin 認得你）
"""
import sqlite3
import time

import pytest

import memory_sandbox

_NOW = time.time()


@pytest.fixture(autouse=True)
def _clean_sandbox_state():
    memory_sandbox.deactivate()
    yield
    memory_sandbox.deactivate()


def _seed_canonical(store_cls, seed_fn, db_path):
    """沙盒關閉下建 store、寫一筆種子資料（＝正本），回關閉連線前的列數。"""
    store = store_cls(db_path=db_path)
    seed_fn(store)
    return store


def _row_count(db_path, table):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


# ── SummaryStore ──────────────────────────────────────────────────────────
def test_summary_store_sandbox_noop(tmp_path):
    from summary_store import SummaryStore
    db = str(tmp_path / "marvin.db")
    _seed_canonical(
        SummaryStore,
        lambda s: s.save_summary(1, _NOW - 10, _NOW, "seed", ["A"]),
        db,
    )
    before = _row_count(db, "session_summaries")

    memory_sandbox.activate()
    sb = SummaryStore(db_path=db)
    # 寫入 no-op、不拋錯
    sb.save_summary(1, 2.0, 3.0, "ghost", ["B"])
    # 讀取仍繼承正本
    assert any(r["summary_text"] == "seed" for r in sb.get_summaries(1))
    # 正本零污染
    assert _row_count(db, "session_summaries") == before


# ── TaskStore ─────────────────────────────────────────────────────────────
def test_task_store_sandbox_noop(tmp_path):
    from task_store import TaskStore
    db = str(tmp_path / "marvin.db")
    _seed_canonical(
        TaskStore,
        lambda s: s.save_task(1, "seed task", "to", "B", "q", 0.0, 1.0, speaker="A"),
        db,
    )
    before = _row_count(db, "tasks")

    memory_sandbox.activate()
    sb = TaskStore(db_path=db)
    sb.save_task(1, "ghost task", "to", "B", "q", 2.0, 3.0, speaker="A")
    sb.update_text(1, "mutated")
    sb.update_status(1, "done")
    assert any("seed task" in t["text"] for t in sb.get_pending(1))
    assert _row_count(db, "tasks") == before


# ── TranscriptStore ───────────────────────────────────────────────────────
def test_transcript_store_sandbox_noop(tmp_path):
    from transcript_store import TranscriptStore
    db = str(tmp_path / "marvin.db")
    _seed_canonical(TranscriptStore, lambda s: s.save("A", 1, "seed line", _NOW), db)
    before = _row_count(db, "transcripts")

    memory_sandbox.activate()
    sb = TranscriptStore(db_path=db)
    sb.save("A", 1, "ghost line", _NOW)
    sb.prune(retention_days=0, now=_NOW + 1e6)  # DELETE 也要 no-op
    assert any("seed line" in r["text"] for r in sb.get_recent(guild_id=1))
    assert _row_count(db, "transcripts") == before


# ── SpeakerTopicGraph ─────────────────────────────────────────────────────
def test_speaker_topic_graph_sandbox_noop(tmp_path):
    from speaker_topic_graph import SpeakerTopicGraph
    db = str(tmp_path / "marvin.db")

    def _seed(s):
        s.record_utterance(speaker="A", channel_id=9, text="seed", ts=100.0)
    _seed_canonical(SpeakerTopicGraph, _seed, db)
    before = _row_count(db, "speaker_topic_graph")

    memory_sandbox.activate()
    sb = SpeakerTopicGraph(db_path=db)
    sb.record_utterance(speaker="B", channel_id=9, text="ghost", ts=200.0)
    sb.set_emotion(transcript_id=1, text_emotion="joy")
    sb.mark_bridged(transcript_id=1)
    assert len(sb.recent(channel_id=9)) >= 1  # 讀得到正本
    assert _row_count(db, "speaker_topic_graph") == before


# ── ProfileCompressor ─────────────────────────────────────────────────────
def test_profile_compressor_sandbox_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-dummy")  # 建 Groq client 用，不實際呼叫
    from profile_compressor import ProfileCompressor
    db = str(tmp_path / "marvin.db")
    seed = ProfileCompressor(db_path=db)
    seed._upsert_profile("A", 1, "seed profile", _NOW)
    before = _row_count(db, "user_profiles")

    memory_sandbox.activate()
    sb = ProfileCompressor(db_path=db)
    sb._upsert_profile("A", 1, "ghost profile", _NOW)  # no-op
    sb._upsert_profile("B", 1, "new ghost", _NOW)      # no-op
    assert sb.get_profile("A", 1) == "seed profile"    # 讀繼承正本
    assert _row_count(db, "user_profiles") == before


# ── SukiBudget ────────────────────────────────────────────────────────────
def test_suki_budget_sandbox_noop(tmp_path):
    from suki_budget import SukiBudget
    db = str(tmp_path / "marvin.db")
    seed = SukiBudget(db_path=db, max_tokens=1000)
    seed.add_tokens(300)  # 正本已花 300
    before_total = seed.tokens

    memory_sandbox.activate()
    sb = SukiBudget(db_path=db, max_tokens=1000)
    # 讀繼承正本當日累積（付費保護代理）
    assert sb.tokens == before_total
    # add_tokens no-op：不寫回正本
    sb.add_tokens(500)
    # 用獨立讀連線確認正本未被沙盒污染
    con = sqlite3.connect(db)
    try:
        row = con.execute("SELECT value FROM budget WHERE key='total_tokens'").fetchone()
    finally:
        con.close()
    import json as _json
    assert _json.loads(row[0]) == before_total

"""SpeakerTopicGraph — Week 1 基建測試。

3 個核心 agent (Bridge / Mood / Ducking) 共用的社交記憶。

設計合約見 docs/social_catalyst_plan.md。

涵蓋：
  1. record_utterance 寫入 + 取回
  2. recent 順序（DESC）
  3. find_similar with embedding（cosine）
  4. find_similar without embedding（keyword fallback）
  5. mark_bridged cooldown 過濾
  6. set_emotion 更新
  7. exclude_speaker / present_speakers 過濾
"""
from __future__ import annotations

import time
import numpy as np
import pytest

from speaker_topic_graph import SpeakerTopicGraph


def _emb(vec: list[float]) -> bytes:
    return np.array(vec, dtype=np.float32).tobytes()


@pytest.fixture
def graph() -> SpeakerTopicGraph:
    return SpeakerTopicGraph(db_path=":memory:")


def test_record_utterance_writes_row_and_recent_returns_desc(graph):
    t0 = time.time()
    graph.record_utterance("alice", 100, "工作好累", ts=t0)
    graph.record_utterance("bob", 100, "下班想喝酒", ts=t0 + 1)
    graph.record_utterance("alice", 100, "主管又在亂", ts=t0 + 2)

    rows = graph.recent(channel_id=100, n=10)
    assert [r["text"] for r in rows] == ["主管又在亂", "下班想喝酒", "工作好累"]
    assert [r["speaker"] for r in rows] == ["alice", "bob", "alice"]


def test_recent_filters_by_channel(graph):
    t0 = time.time()
    graph.record_utterance("alice", 100, "channel A", ts=t0)
    graph.record_utterance("alice", 200, "channel B", ts=t0 + 1)

    rows_a = graph.recent(channel_id=100, n=10)
    rows_b = graph.recent(channel_id=200, n=10)
    assert len(rows_a) == 1 and rows_a[0]["text"] == "channel A"
    assert len(rows_b) == 1 and rows_b[0]["text"] == "channel B"


def test_find_similar_with_embedding_ranks_by_cosine(graph):
    t0 = time.time()
    # alice 講了壓力相關（emb 接近 query），bob 講了食物（emb 遠）
    graph.record_utterance("alice", 100, "主管很煩", embedding=_emb([1.0, 0.0, 0.0]), ts=t0)
    graph.record_utterance("bob", 100, "晚餐吃什麼", embedding=_emb([0.0, 1.0, 0.0]), ts=t0 + 1)
    graph.record_utterance("alice", 100, "壓力大", embedding=_emb([0.9, 0.1, 0.0]), ts=t0 + 2)

    query = _emb([1.0, 0.0, 0.0])
    results = graph.find_similar(
        query_embedding=query,
        channel_id=100,
        exclude_speaker="charlie",
        min_similarity=0.5,
    )
    # 兩個 alice 的話題都該命中，bob 的不該
    texts = [r["text"] for r in results]
    assert "主管很煩" in texts
    assert "壓力大" in texts
    assert "晚餐吃什麼" not in texts
    # 排序：similarity DESC
    sims = [r["similarity"] for r in results]
    assert sims == sorted(sims, reverse=True)


def test_find_similar_excludes_speaker(graph):
    t0 = time.time()
    graph.record_utterance("alice", 100, "壓力", embedding=_emb([1.0, 0.0]), ts=t0)
    graph.record_utterance("bob", 100, "壓力", embedding=_emb([1.0, 0.0]), ts=t0 + 1)

    results = graph.find_similar(
        query_embedding=_emb([1.0, 0.0]),
        channel_id=100,
        exclude_speaker="alice",
    )
    speakers = [r["speaker"] for r in results]
    assert "alice" not in speakers
    assert "bob" in speakers


def test_find_similar_filters_to_present_speakers(graph):
    t0 = time.time()
    graph.record_utterance("alice", 100, "壓力 1", embedding=_emb([1.0, 0.0]), ts=t0)
    graph.record_utterance("bob", 100, "壓力 2", embedding=_emb([1.0, 0.0]), ts=t0 + 1)
    graph.record_utterance("charlie", 100, "壓力 3", embedding=_emb([1.0, 0.0]), ts=t0 + 2)

    results = graph.find_similar(
        query_embedding=_emb([1.0, 0.0]),
        channel_id=100,
        exclude_speaker="dave",
        present_speakers={"alice", "bob"},
    )
    speakers = {r["speaker"] for r in results}
    assert speakers <= {"alice", "bob"}
    assert "charlie" not in speakers


def test_find_similar_keyword_fallback_when_no_embedding(graph):
    """無 embedding 時退回 keyword overlap，仍然能找出話題相近的句子。"""
    t0 = time.time()
    graph.record_utterance("alice", 100, "主管亂罵", ts=t0)
    graph.record_utterance("bob", 100, "吃午餐", ts=t0 + 1)

    results = graph.find_similar_by_text(
        query_text="主管壓力",
        channel_id=100,
        exclude_speaker="charlie",
    )
    texts = [r["text"] for r in results]
    assert "主管亂罵" in texts
    # bob 的食物句子 keyword 沒重疊，不該出現（或者排在很後面）
    if "吃午餐" in texts:
        idx_alice = texts.index("主管亂罵")
        idx_bob = texts.index("吃午餐")
        assert idx_alice < idx_bob


def test_mark_bridged_excludes_from_future_finds(graph):
    t0 = time.time()
    graph.record_utterance("alice", 100, "壓力", embedding=_emb([1.0, 0.0]), ts=t0)
    graph.record_utterance("alice", 100, "煩躁", embedding=_emb([1.0, 0.0]), ts=t0 + 1)

    # 先找一次拿到 transcript_id
    results = graph.find_similar(
        query_embedding=_emb([1.0, 0.0]),
        channel_id=100,
        exclude_speaker="bob",
    )
    assert len(results) == 2
    bridge_target_id = results[0]["transcript_id"]
    graph.mark_bridged(bridge_target_id)

    # 在 cooldown 內找應該排除被 bridge 過的
    results2 = graph.find_similar(
        query_embedding=_emb([1.0, 0.0]),
        channel_id=100,
        exclude_speaker="bob",
        cooldown_days=30,
    )
    ids = {r["transcript_id"] for r in results2}
    assert bridge_target_id not in ids


def test_set_emotion_updates_row(graph):
    t0 = time.time()
    graph.record_utterance("alice", 100, "好累", ts=t0)
    rows = graph.recent(channel_id=100, n=1)
    tid = rows[0]["transcript_id"]

    graph.set_emotion(tid, text_emotion="低落", prosody_emotion="低能量")
    rows = graph.recent(channel_id=100, n=1)
    assert rows[0]["emotion_text"] == "低落"
    assert rows[0]["emotion_prosody"] == "低能量"


def test_record_empty_text_is_noop(graph):
    """空白文字不該寫入，避免污染社交圖。"""
    t0 = time.time()
    graph.record_utterance("alice", 100, "   ", ts=t0)
    graph.record_utterance("alice", 100, "", ts=t0 + 1)
    rows = graph.recent(channel_id=100, n=10)
    assert rows == []

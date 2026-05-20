"""End-to-end integration test：從 jsonl + transcript 跑完整 offline pipeline。

單測已驗各模組獨立行為，這份測 wiring：
  Recommendation.append → CLI run() → Batch → Analyzer → Writer (T1+T2+T3) → reports

Mock LLM client（避免真打 Groq），其他全部用真實模組（in-memory 替身）。
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_agents.feedback_analyzer import MusicFeedbackAnalyzer
from intent_agents.recommendation import Recommendation, append_recommendation


# ── Fake stores（in-memory，行為 mirror 真實 stores 的關鍵 API） ────────

class _FakeMusicMemory:
    """In-memory mirror of MusicMemory's add_recommendation_feedback +
    get_recent_feedback methods used by the pipeline."""

    def __init__(self):
        self.recommendations: dict[str, list[dict]] = {}

    def add_recommendation_feedback(self, username: str, title: str, result: str):
        bucket = self.recommendations.setdefault(username, [])
        # Use real time.time() so T2's "now - 30d window" includes these entries
        bucket.append({"title": title, "result": result, "ts": time.time()})

    def get_recent_feedback(self, username: str, since_ts: float) -> list[dict]:
        return [
            e for e in self.recommendations.get(username, [])
            if e.get("ts", 0) >= since_ts
        ]


class _FakeSuki:
    """In-memory mirror of MemoryManager's has_player + update_player_memory."""

    def __init__(self, known_players: set[str]):
        self.players: dict[str, dict] = {
            name: {"likes": [], "dislikes": [], "taboos": []}
            for name in known_players
        }

    def has_player(self, username: str) -> bool:
        return username in self.players

    def update_player_memory(self, username: str, new_info: dict):
        if username not in self.players:
            return
        for key in ("likes", "dislikes", "taboos"):
            if key in new_info and isinstance(new_info[key], list):
                cur = set(self.players[username].get(key, []))
                cur.update(item for item in new_info[key] if item)
                self.players[username][key] = list(cur)


class _FakeTranscriptStore:
    """In-memory transcript store. Mirrors get_recent() signature for the adapter."""

    def __init__(self, utts: list[dict]):
        self.utts = utts

    def get_recent(self, speaker=None, guild_id=0, days=7, minutes=None):
        # Adapter calls with days large enough — just return everything matching speaker
        if speaker is None:
            return list(self.utts)
        return [u for u in self.utts if u["speaker"] == speaker]


# ── LLM client mock helpers ───────────────────────────────────────────────

def _llm_client_returning_sentiments(sentiments_by_call: list[dict]):
    """Build a mock LLM that returns each sentiment JSON in sequence."""
    responses = []
    for s in sentiments_by_call:
        payload = {
            "sentiment": s.get("sentiment", "positive"),
            "confidence": s.get("confidence", 0.85),
            "reason": s.get("reason", "mock"),
            "evidence": s.get("evidence", []),
        }
        responses.append(SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps(payload, ensure_ascii=False)
            ))],
            usage=SimpleNamespace(total_tokens=80),
        ))
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=responses)
    return client


def _ts_for_date(date_str: str, hour: int = 14) -> float:
    return datetime.strptime(date_str, "%Y-%m-%d").replace(hour=hour).timestamp()


# ── Tests ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_pipeline_three_recs_one_promotes_to_likes(tmp_path):
    """大肚 對「周杰倫 夜曲」連續 3 次 positive → T1 全寫 + T2 推進 suki.likes。
    另一個 user 露的 rec 不該影響 大肚 的 promotion。"""
    from scripts.analyze_daily_feedback import run

    date = "2026-05-19"
    ts_base = _ts_for_date(date)
    rec_log = tmp_path / "rec.jsonl"

    # 3 同向 recs for 大肚 + 1 unrelated for 露
    for i in range(3):
        append_recommendation(
            Recommendation(
                ts=ts_base + i * 60, agent="music", speaker="大肚",
                trigger="queue_empty", selected="周杰倫 夜曲",
                reason_internal="r", explanation_uttered="e",
                feedback_window_s=300, channel_state={},
            ),
            path=rec_log,
        )
    append_recommendation(
        Recommendation(
            ts=ts_base + 200, agent="music", speaker="露",
            trigger="queue_empty", selected="孫燕姿 遇見",
            reason_internal="r", explanation_uttered="e",
            feedback_window_s=300, channel_state={},
        ),
        path=rec_log,
    )

    # User utts: 大肚 都好評；露 negative（不該污染 大肚）
    transcript_store = _FakeTranscriptStore([
        {"speaker": "大肚", "text": "好聽", "timestamp": ts_base + 30},
        {"speaker": "大肚", "text": "讚", "timestamp": ts_base + 90},
        {"speaker": "大肚", "text": "再來一首", "timestamp": ts_base + 150},
        {"speaker": "露", "text": "不要", "timestamp": ts_base + 230},
    ])
    music_memory = _FakeMusicMemory()
    suki = _FakeSuki(known_players={"大肚", "露"})

    llm = _llm_client_returning_sentiments([
        {"sentiment": "positive", "confidence": 0.9, "reason": "好聽"},
        {"sentiment": "positive", "confidence": 0.9, "reason": "讚"},
        {"sentiment": "positive", "confidence": 0.9, "reason": "再來一首"},
        {"sentiment": "negative", "confidence": 0.85, "reason": "不要"},
    ])

    summary = await run(
        date,
        recs_path=rec_log,
        output_dir=tmp_path,
        music_memory=music_memory,
        suki_memory=suki,
        transcript_store=transcript_store,
        analyzers={"music": MusicFeedbackAnalyzer(llm_client=llm)},
    )

    # T1: 4 個 rec 全寫進 music_memory
    assert summary["total"] == 4
    assert len(music_memory.recommendations["大肚"]) == 3
    assert len(music_memory.recommendations["露"]) == 1

    # T2: 大肚 命中 ≥3 同向 → likes 加「周杰倫 夜曲」
    # 露 只 1 次不夠 threshold → 不加 dislikes
    assert summary["t2_promotions"] == 1
    assert "周杰倫 夜曲" in suki.players["大肚"]["likes"]
    assert "孫燕姿 遇見" not in suki.players["露"]["dislikes"]

    # 報告檔產出
    analysis = (tmp_path / f"feedback_analysis_{date}.md").read_text(encoding="utf-8")
    audit = (tmp_path / f"audit_{date}.md").read_text(encoding="utf-8")
    assert "周杰倫 夜曲" in analysis
    assert "positive: 3" in analysis  # sentiment breakdown
    assert "negative: 1" in analysis
    # 高信心無錯誤 → audit 應該乾淨
    assert "No anomalies" in audit or "0" in audit


@pytest.mark.asyncio
async def test_full_pipeline_low_confidence_skipped_from_both_t1_t2(tmp_path):
    """低 confidence → T1 不寫、T2 不算、T3 audit 有紀錄。"""
    from scripts.analyze_daily_feedback import run

    date = "2026-05-19"
    ts_base = _ts_for_date(date)
    rec_log = tmp_path / "rec.jsonl"
    append_recommendation(
        Recommendation(
            ts=ts_base, agent="music", speaker="大肚",
            trigger="queue_empty", selected="周杰倫 夜曲",
            reason_internal="r", explanation_uttered="e",
            feedback_window_s=300, channel_state={},
        ),
        path=rec_log,
    )

    transcript_store = _FakeTranscriptStore([
        {"speaker": "大肚", "text": "嗯...", "timestamp": ts_base + 30},
    ])
    music_memory = _FakeMusicMemory()
    suki = _FakeSuki(known_players={"大肚"})

    llm = _llm_client_returning_sentiments([
        {"sentiment": "positive", "confidence": 0.2, "reason": "不確定"},
    ])

    summary = await run(
        date,
        recs_path=rec_log,
        output_dir=tmp_path,
        music_memory=music_memory,
        suki_memory=suki,
        transcript_store=transcript_store,
        analyzers={"music": MusicFeedbackAnalyzer(llm_client=llm)},
    )

    # T1 skipped（confidence < 0.5）
    assert music_memory.recommendations == {}
    # T2 skipped（同上 confidence 低）
    assert summary["t2_promotions"] == 0
    assert suki.players["大肚"]["likes"] == []
    # T3 有 audit line
    assert summary["audit_lines"] == 1
    audit = (tmp_path / f"audit_{date}.md").read_text(encoding="utf-8")
    assert "low_confidence" in audit


@pytest.mark.asyncio
async def test_full_pipeline_dry_run_no_store_writes(tmp_path):
    """dry-run：報告產出但不寫 store。"""
    from scripts.analyze_daily_feedback import run

    date = "2026-05-19"
    ts_base = _ts_for_date(date)
    rec_log = tmp_path / "rec.jsonl"
    for i in range(3):
        append_recommendation(
            Recommendation(
                ts=ts_base + i * 60, agent="music", speaker="大肚",
                trigger="queue_empty", selected="周杰倫 夜曲",
                reason_internal="r", explanation_uttered="e",
                feedback_window_s=300, channel_state={},
            ),
            path=rec_log,
        )

    transcript_store = _FakeTranscriptStore([])  # 沉默 = positive
    music_memory = _FakeMusicMemory()
    suki = _FakeSuki(known_players={"大肚"})

    summary = await run(
        date,
        recs_path=rec_log,
        output_dir=tmp_path,
        music_memory=music_memory,
        suki_memory=suki,
        transcript_store=transcript_store,
        analyzers={"music": MusicFeedbackAnalyzer(llm_client=MagicMock())},
        dry_run=True,
    )

    assert summary["dry_run"] is True
    assert summary["total"] == 3
    # Reports yes
    assert (tmp_path / f"feedback_analysis_{date}.md").exists()
    # Store writes no
    assert music_memory.recommendations == {}
    assert suki.players["大肚"]["likes"] == []

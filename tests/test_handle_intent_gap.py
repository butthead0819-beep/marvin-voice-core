"""handle_intent_gap orchestrator — bus 沒贏家 + has_intent_signal=true 的處理流程。

四條路徑：
 - happy：classifier OK + fresh pair → 寫 acknowledged=True + 播 TTS + mark_acked
 - dedup-skipped：classifier OK + 5min 內已 ack 過 → 寫 acknowledged=False + 不播
 - UNKNOWN from LLM：寫 intent_type=UNKNOWN + acknowledged=False + 不播
 - classifier raise：catch → 寫 UNKNOWN gap + 不播

額外：LLM 回 ack_text=null（intent 非 UNKNOWN 但 LLM 自己覺得不該 ack）也是不播。
"""
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from intent_bus import IntentContext
from intent_gap import GapLogger, handle_intent_gap


def _ctx(speaker="alice", query="播 showay 點過的歌", now=1000.0):
    return IntentContext(
        speaker=speaker,
        raw_text=query,
        query=query,
        original_raw=query,
        wake_intent=0.8,
        stream_active=False,
        game_mode=False,
        is_owner=False,
        now=now,
        mode="normal",
    )


def _manifest():
    return {
        "version": "2026-05-27",
        "agents": [
            {"name": "music_v2", "intents": [
                {"name": "play_song", "required_slots": ["song_choice"], "reason_template": "play_song:{song_choice}"},
            ]},
        ],
    }


def _classifier_returning(payload):
    return AsyncMock(return_value=payload)


def _classifier_raising(exc):
    return AsyncMock(side_effect=exc)


# ── Path 1: happy path ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_happy_path_writes_ack_record_and_plays_tts(tmp_path: Path):
    classifier = _classifier_returning({
        "intent_type": "replay_user_history",
        "slots": {"target_user": "showay"},
        "nearest_agent": "music_v2",
        "nearest_distance": 0.45,
        "ack_text": "想播 showay 點過的歌，這個還沒會。",
    })
    gap_logger = GapLogger(tmp_path / "gaps.jsonl")
    tts = AsyncMock()

    await handle_intent_gap(
        _ctx(),
        utterance_id="u-1",
        classifier=classifier,
        gap_logger=gap_logger,
        manifest=_manifest(),
        tts_call=tts,
    )

    # JSONL 一筆，acknowledged=True
    line = (tmp_path / "gaps.jsonl").read_text(encoding="utf-8").splitlines()[0]
    rec = json.loads(line)
    assert rec["intent_type"] == "replay_user_history"
    assert rec["acknowledged"] is True
    assert rec["ack_text"] == "想播 showay 點過的歌，這個還沒會。"
    assert rec["utterance_id"] == "u-1"
    assert rec["nearest_agent"] == "music_v2"

    # TTS 被呼叫
    tts.assert_awaited_once_with("想播 showay 點過的歌，這個還沒會。")


@pytest.mark.asyncio
async def test_happy_path_marks_acked_in_cache(tmp_path: Path):
    """ack 後 cache 留紀錄，第二次 should_ack 回 False。"""
    classifier = _classifier_returning({
        "intent_type": "replay_user_history",
        "slots": {},
        "nearest_agent": "music_v2",
        "nearest_distance": 0.4,
        "ack_text": "這個還沒會。",
    })
    gap_logger = GapLogger(tmp_path / "gaps.jsonl")

    await handle_intent_gap(
        _ctx(now=1000.0),
        utterance_id="u-1",
        classifier=classifier,
        gap_logger=gap_logger,
        manifest=_manifest(),
        tts_call=AsyncMock(),
    )

    # 5min 內同 (speaker, intent_type) 不再 ack
    assert gap_logger.should_ack("alice", "replay_user_history", now=1100.0) is False


# ── Path 2: dedup-skipped ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dedup_skipped_writes_unacked_record_no_tts(tmp_path: Path):
    classifier = _classifier_returning({
        "intent_type": "replay_user_history",
        "slots": {"target_user": "showay"},
        "nearest_agent": "music_v2",
        "nearest_distance": 0.45,
        "ack_text": "想播 showay 點過的歌。",
    })
    gap_logger = GapLogger(tmp_path / "gaps.jsonl")
    # 預先標記 4 分鐘前已 ack 過
    gap_logger.mark_acked("alice", "replay_user_history", now=760.0)
    tts = AsyncMock()

    await handle_intent_gap(
        _ctx(now=1000.0),
        utterance_id="u-2",
        classifier=classifier,
        gap_logger=gap_logger,
        manifest=_manifest(),
        tts_call=tts,
    )

    rec = json.loads((tmp_path / "gaps.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert rec["intent_type"] == "replay_user_history"
    assert rec["acknowledged"] is False
    assert rec["ack_text"] is None  # dedup-skipped 不留 ack_text
    assert rec["nearest_agent"] == "music_v2"  # 但其他欄位完整保留

    tts.assert_not_awaited()


# ── Path 3: LLM 自己回 UNKNOWN ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_unknown_writes_unknown_record_no_tts(tmp_path: Path):
    classifier = _classifier_returning({
        "intent_type": "UNKNOWN",
        "slots": {},
        "nearest_agent": None,
        "nearest_distance": None,
        "ack_text": None,
    })
    gap_logger = GapLogger(tmp_path / "gaps.jsonl")
    tts = AsyncMock()

    await handle_intent_gap(
        _ctx(query="嗯啊"),
        utterance_id="u-3",
        classifier=classifier,
        gap_logger=gap_logger,
        manifest=_manifest(),
        tts_call=tts,
    )

    rec = json.loads((tmp_path / "gaps.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert rec["intent_type"] == "UNKNOWN"
    assert rec["acknowledged"] is False
    assert rec["ack_text"] is None

    tts.assert_not_awaited()
    # UNKNOWN 不該污染 ack cache
    assert gap_logger._last_ack == {}


# ── Path 4: classifier raise → UNKNOWN gap ──────────────────────────────────

@pytest.mark.asyncio
async def test_classifier_raise_writes_unknown_record_no_tts(tmp_path: Path):
    """JSON 解析失敗或 router 例外 → catch、寫 UNKNOWN gap、不播 ack（拍板 #5）。"""
    classifier = _classifier_raising(json.JSONDecodeError("bad", "x", 0))
    gap_logger = GapLogger(tmp_path / "gaps.jsonl")
    tts = AsyncMock()

    await handle_intent_gap(
        _ctx(),
        utterance_id="u-4",
        classifier=classifier,
        gap_logger=gap_logger,
        manifest=_manifest(),
        tts_call=tts,
    )

    rec = json.loads((tmp_path / "gaps.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert rec["intent_type"] == "UNKNOWN"
    assert rec["acknowledged"] is False
    assert rec["ack_text"] is None
    # 原 query 還是要保留，daily ritual 才看得到「LLM 在這句話炸了」
    assert rec["raw_query"] == "播 showay 點過的歌"

    tts.assert_not_awaited()


# ── Return value：caller (voice_controller) 用 intent_type 判 fall-through ──

@pytest.mark.asyncio
async def test_returns_record_with_known_intent_for_non_unknown(tmp_path: Path):
    """非 UNKNOWN → caller 看到後 return，不打 Marvin。"""
    classifier = _classifier_returning({
        "intent_type": "replay_user_history", "slots": {},
        "nearest_agent": "music_v2", "nearest_distance": 0.4,
        "ack_text": "這個還沒會。",
    })
    rec = await handle_intent_gap(
        _ctx(), utterance_id="u-r1",
        classifier=classifier, gap_logger=GapLogger(tmp_path / "g.jsonl"),
        manifest=_manifest(), tts_call=AsyncMock(),
    )
    assert rec.intent_type == "replay_user_history"
    assert rec.acknowledged is True


@pytest.mark.asyncio
async def test_returns_record_with_unknown_intent_for_fall_through(tmp_path: Path):
    """UNKNOWN → caller fall through 到 Marvin LLM。"""
    classifier = _classifier_returning({
        "intent_type": "UNKNOWN", "slots": {},
        "nearest_agent": None, "nearest_distance": None, "ack_text": None,
    })
    rec = await handle_intent_gap(
        _ctx(query="嗯啊"), utterance_id="u-r2",
        classifier=classifier, gap_logger=GapLogger(tmp_path / "g.jsonl"),
        manifest=_manifest(), tts_call=AsyncMock(),
    )
    assert rec.intent_type == "UNKNOWN"
    assert rec.acknowledged is False


# ── Edge: LLM 回 well-formed 但 ack_text=None ────────────────────────────────

@pytest.mark.asyncio
async def test_llm_returns_ack_text_none_no_tts(tmp_path: Path):
    """intent_type 非 UNKNOWN 但 LLM 自己給 ack_text=null → 仍不播。"""
    classifier = _classifier_returning({
        "intent_type": "some_intent",
        "slots": {},
        "nearest_agent": "music_v2",
        "nearest_distance": 0.8,
        "ack_text": None,
    })
    gap_logger = GapLogger(tmp_path / "gaps.jsonl")
    tts = AsyncMock()

    await handle_intent_gap(
        _ctx(),
        utterance_id="u-5",
        classifier=classifier,
        gap_logger=gap_logger,
        manifest=_manifest(),
        tts_call=tts,
    )

    rec = json.loads((tmp_path / "gaps.jsonl").read_text(encoding="utf-8").splitlines()[0])
    # intent_type 保留（daily ritual 看得到「LLM 認出 intent 但選擇不 ack」）
    assert rec["intent_type"] == "some_intent"
    assert rec["acknowledged"] is False
    assert rec["ack_text"] is None

    tts.assert_not_awaited()

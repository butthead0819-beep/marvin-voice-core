"""Audio rescue wav sidecar — 每次嘗試都存（2026-10-22）。

背景：`records/rescue_wav/` 原本只在 rescue **成功執行**（dispatch_source ==
"llm_rescue_audio"）時才存音檔，just_chatting / 逾時 / 放棄的嘗試完全沒留下
wav。下一輪方向判斷評估需要這些「沒救成」的案例，所以條件改成只看
`original_ctx.audio_wav_bytes` 有沒有值，不再管 rescued_ctx 是否命中
（見 `_emit_rescue_outcome`）。STT 原文本來就在 rescue_outcomes.jsonl 裡，
這裡只是補音檔。
"""
from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from intent_bus import Bid, IntentBus, IntentContext


def _ctx(query="希望下次可以找到好聽的歌", **kwargs):
    base = dict(
        speaker="Alice", raw_text=query, query=query, original_raw=query,
        wake_intent=0.9, stream_active=False, game_mode=False,
        is_owner=False, now=12345.0, mode="normal", depth=0,
    )
    base.update(kwargs)
    return IntentContext(**base)


class _StubAgent:
    def __init__(self, name, bid_fn):
        self.name = name
        self._bid_fn = bid_fn

    def bid(self, ctx):
        return self._bid_fn(ctx)


class _StubRescue:
    name = "AudioRescue"

    def __init__(self, result, abandon_reason=None):
        self._result = result
        self.last_abandon_reason = abandon_reason

    async def synthesize(self, ctx):
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


def _all_zero(ctx):
    return Bid(name="z", confidence=0.0, handler=AsyncMock(), reason="no_match")


@pytest.mark.asyncio
async def test_abandoned_attempt_still_writes_wav(tmp_path, monkeypatch):
    """synthesize 放棄（just_chatting）但原始音訊存在 → 仍寫 wav sidecar。"""
    monkeypatch.chdir(tmp_path)
    from intent_agents.rescue_outcome_logger import RescueWavStore

    orig = _ctx(audio_wav_bytes=b"RIFFabandoned", now=1787211701.438)
    records = []
    bus = IntentBus(
        [_StubAgent("z", _all_zero)],
        llm_rescue_agent=_StubRescue(result=None, abandon_reason="just_chatting"),
        rescue_outcome_sink=records.append,
        rescue_wav_store=RescueWavStore(tmp_path / "records" / "rescue_wav"),
    )
    await bus.dispatch(orig)

    assert len(records) == 1
    r = records[0]
    assert r["gap_class"] == "abandoned"
    assert r["abandon_reason"] == "just_chatting"
    wav_path = r["wav_path"]
    assert (tmp_path / wav_path).read_bytes() == b"RIFFabandoned"


@pytest.mark.asyncio
async def test_successful_rescue_still_writes_wav(tmp_path, monkeypatch):
    """既有成功路徑回歸：命中 agent 一樣要寫 wav。"""
    monkeypatch.chdir(tmp_path)
    from intent_agents.rescue_outcome_logger import RescueWavStore

    volume_agent = _StubAgent(
        "volume",
        lambda c: Bid(name="volume", confidence=0.0, handler=AsyncMock(), reason="no_match"),
    )
    volume_agent.resolve_intent = lambda name, slots, ctx: Bid(
        name="volume", confidence=0.9, handler=AsyncMock(), reason="audio_rescue:x"
    )

    rescued_ctx = replace(
        _ctx(), depth=1, dispatch_source="llm_rescue_audio",
        resolved_agent="volume", resolved_intent="volume_down", resolved_slots={},
    )
    orig = _ctx(audio_wav_bytes=b"RIFFsuccess", now=1787211701.438)
    records = []
    bus = IntentBus(
        [volume_agent],
        llm_rescue_agent=_StubRescue(result=rescued_ctx),
        rescue_outcome_sink=records.append,
        rescue_wav_store=RescueWavStore(tmp_path / "records" / "rescue_wav"),
    )
    await bus.dispatch(orig)

    assert len(records) == 1
    wav_path = records[0]["wav_path"]
    assert (tmp_path / wav_path).read_bytes() == b"RIFFsuccess"


@pytest.mark.asyncio
async def test_no_audio_bytes_writes_no_wav(tmp_path, monkeypatch):
    """沒有原始音訊（文字版 rescue）→ 不寫 wav。"""
    monkeypatch.chdir(tmp_path)
    from intent_agents.rescue_outcome_logger import RescueWavStore

    orig = _ctx()  # audio_wav_bytes 預設 None
    records = []
    bus = IntentBus(
        [_StubAgent("z", _all_zero)],
        llm_rescue_agent=_StubRescue(result=None, abandon_reason="just_chatting"),
        rescue_outcome_sink=records.append,
        rescue_wav_store=RescueWavStore(tmp_path / "records" / "rescue_wav"),
    )
    await bus.dispatch(orig)

    assert "wav_path" not in records[0]
    assert not (tmp_path / "records" / "rescue_wav").exists()


@pytest.mark.asyncio
async def test_no_wav_store_does_not_crash():
    """rescue_wav_store=None（env 沒開 audio mode）→ 不寫、不炸。"""
    orig = _ctx(audio_wav_bytes=b"RIFFnowstore")
    records = []
    bus = IntentBus(
        [_StubAgent("z", _all_zero)],
        llm_rescue_agent=_StubRescue(result=None, abandon_reason="just_chatting"),
        rescue_outcome_sink=records.append,
        # rescue_wav_store 不傳 → None
    )
    await bus.dispatch(orig)

    assert "wav_path" not in records[0]

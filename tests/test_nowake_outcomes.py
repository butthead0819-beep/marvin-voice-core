"""TDD：nowake_outcomes — nowake 分派結果落地，不污染 judge_outcomes.jsonl。"""
from __future__ import annotations

import json
import types

import pytest

from nowake_outcomes import build_nowake_row, append_nowake_outcome, SCHEMA_VERSION
from intent_bus import IntentBus, IntentContext, Bid


def _ctx(**kw):
    base = dict(speaker="Alice", raw_text="今天天氣怎樣", query="今天天氣怎樣",
                original_raw=None, wake_intent=None, stream_active=False,
                game_mode=False, is_owner=False, now=100.0, dispatch_source="nowake")
    base.update(kw)
    return types.SimpleNamespace(**base)


def _bid(name, confidence, reason="test"):
    return types.SimpleNamespace(name=name, confidence=confidence, reason=reason)


# ── build_nowake_row：純函式 ─────────────────────────────────────────────────

def test_build_nowake_row_with_winner():
    ctx = _ctx()
    winner = _bid("now_playing", 0.9)
    bids = [winner]
    row = build_nowake_row(ctx, bids, winner, now=123.0)

    assert row["ts"] == 123.0
    assert row["speaker"] == "Alice"
    assert row["raw_text"] == "今天天氣怎樣"
    assert row["query"] == "今天天氣怎樣"
    assert row["winner"] == "now_playing"
    assert row["winner_confidence"] == 0.9
    assert row["schema_version"] == SCHEMA_VERSION


def test_build_nowake_row_no_winner():
    ctx = _ctx()
    row = build_nowake_row(ctx, [], None, now=1.0)
    assert row["winner"] is None
    assert row["winner_confidence"] is None


def test_build_nowake_row_bids_sorted_by_confidence_desc():
    ctx = _ctx()
    low = _bid("low", 0.3)
    high = _bid("high", 0.9)
    mid = _bid("mid", 0.5)
    row = build_nowake_row(ctx, [low, high, mid], high, now=1.0)
    assert [b["name"] for b in row["bids"]] == ["high", "mid", "low"]


def test_build_nowake_row_raw_text_none_becomes_empty_string():
    ctx = _ctx(raw_text=None)
    row = build_nowake_row(ctx, [], None, now=1.0)
    assert row["raw_text"] == ""


# ── append_nowake_outcome：IO ────────────────────────────────────────────────

def test_append_nowake_outcome_writes_line(tmp_path):
    path = tmp_path / "nowake_outcomes.jsonl"
    ctx = _ctx(raw_text="放告五人的歌", query="放告五人的歌")
    append_nowake_outcome(ctx, [], None, path=path, now=1.0)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["raw_text"] == "放告五人的歌"
    assert "\\u" not in lines[0]  # ensure_ascii=False，中文原樣


def test_append_nowake_outcome_relative_path_noop_under_pytest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ctx = _ctx()
    append_nowake_outcome(ctx, [], None, path="records/x.jsonl", now=1.0)
    assert not (tmp_path / "records" / "x.jsonl").exists()


def test_append_nowake_outcome_swallows_write_errors(tmp_path):
    # path 指向一個已存在的目錄 → open() 會炸，不該往上拋
    dir_as_path = tmp_path / "im_a_dir"
    dir_as_path.mkdir()
    ctx = _ctx()
    append_nowake_outcome(ctx, [], None, path=dir_as_path, now=1.0)  # 不拋例外即通過


# ── IntentBus 整合：三個出口只有 nowake 才記 ──────────────────────────────────

class _StubAgent:
    def __init__(self, name, bid_fn=None):
        self.name = name
        self._bid_fn = bid_fn

    def bid(self, ctx):
        return self._bid_fn(ctx) if self._bid_fn else None


def _real_bid(name, confidence, handler=None):
    async def _default():
        return None
    return Bid(name=name, confidence=confidence, handler=handler or _default, reason=f"test:{name}")


def _bus_ctx(dispatch_source):
    return IntentContext(
        speaker="Alice",
        raw_text="放告五人的歌",
        query="放告五人的歌",
        original_raw=None,
        wake_intent=None,
        stream_active=False,
        game_mode=False,
        is_owner=False,
        now=100.0,
        dispatch_source=dispatch_source,
    )


@pytest.mark.asyncio
async def test_nowake_dispatch_with_winner_records_once(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "nowake_outcomes.append_nowake_outcome",
        lambda ctx, bids, winner, **kw: calls.append((ctx, bids, winner)),
    )
    bus = IntentBus([_StubAgent("a", lambda c: _real_bid("a", 0.9))])
    ctx = _bus_ctx("nowake")
    winner = await bus.dispatch(ctx)

    assert len(calls) == 1
    _, _, recorded_winner = calls[0]
    assert recorded_winner is winner
    assert recorded_winner.name == "a"


@pytest.mark.asyncio
async def test_nowake_dispatch_below_threshold_records_none_winner(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "nowake_outcomes.append_nowake_outcome",
        lambda ctx, bids, winner, **kw: calls.append((ctx, bids, winner)),
    )
    bus = IntentBus([_StubAgent("weak", lambda c: _real_bid("weak", 0.1))])
    ctx = _bus_ctx("nowake")
    result = await bus.dispatch(ctx)

    assert result is None
    assert len(calls) == 1
    assert calls[0][2] is None


@pytest.mark.asyncio
async def test_non_nowake_dispatch_source_never_records(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "nowake_outcomes.append_nowake_outcome",
        lambda ctx, bids, winner, **kw: calls.append((ctx, bids, winner)),
    )
    bus = IntentBus([_StubAgent("a", lambda c: _real_bid("a", 0.9))])
    ctx = _bus_ctx("wake_shortcut")
    await bus.dispatch(ctx)

    assert calls == []

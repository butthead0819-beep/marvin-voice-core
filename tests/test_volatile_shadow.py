"""Volatile Results Phase 0 影子量測（2026-06-13）。

stream_stt_shadow_bin 以實時節奏重播 utterance WAV，輸出 volatile 時間線；
本模組解析時間線並計算決策指標：
- stable_ms：文字最後一次變動的時刻（語意斷句的潛在切點）
- n_revisions：假設翻盤次數（新文字不是舊文字的延伸＝翻盤）
- wake_first_ms：喚醒詞最早可見時刻（volatile arm 的潛在收益）

零管線影響：取樣 + fire-and-forget 重播，記 records/volatile_shadow.jsonl。
"""
from __future__ import annotations

import json

import pytest

import volatile_shadow


def _ev(t_ms: int, text: str) -> dict:
    return {"t_ms": t_ms, "text": text, "start_s": 0.0, "end_s": 0.0, "fin_s": 0.0}


# ── parse_events ───────────────────────────────────────────────────────────

def test_parse_events_extracts_events_and_done():
    lines = [
        json.dumps(_ev(100, "馬")),
        "garbage not json",
        json.dumps(_ev(200, "馬文")),
        '__DONE__ {"audio_ms": 4000, "wall_ms": 4300}',
    ]
    events, done = volatile_shadow.parse_events(lines)
    assert len(events) == 2
    assert done["audio_ms"] == 4000


# ── analyze_timeline ───────────────────────────────────────────────────────

def test_stable_ms_is_last_text_change():
    events = [_ev(100, "馬"), _ev(500, "馬文播歌"), _ev(900, "馬文播歌"), _ev(1200, "馬文播歌")]
    out = volatile_shadow.analyze_timeline(events, audio_ms=3000)
    assert out["stable_ms"] == 500
    assert out["final_text"] == "馬文播歌"


def test_revisions_counted_when_text_not_extension():
    """「馬聞」→「馬文幫」不是延伸＝翻盤一次；純延伸不算。"""
    events = [_ev(100, "馬"), _ev(200, "馬聞"), _ev(400, "馬文幫"), _ev(600, "馬文幫我")]
    out = volatile_shadow.analyze_timeline(events, audio_ms=2000)
    assert out["n_revisions"] == 1


def test_wake_first_ms_detects_earliest_wake_variant():
    events = [_ev(100, "馬"), _ev(300, "毛文"), _ev(700, "毛文播放")]
    out = volatile_shadow.analyze_timeline(events, audio_ms=2000)
    assert out["wake_first_ms"] == 300  # 毛文在 wake 清單（6/13 補）


def test_wake_first_ms_none_when_no_wake():
    events = [_ev(100, "今天"), _ev(300, "今天天氣")]
    out = volatile_shadow.analyze_timeline(events, audio_ms=1000)
    assert out["wake_first_ms"] is None


def test_empty_events_safe():
    out = volatile_shadow.analyze_timeline([], audio_ms=1000)
    assert out["final_text"] == ""
    assert out["stable_ms"] is None


# ── run_replay 寫檔（注入 fake runner）────────────────────────────────────

@pytest.mark.asyncio
async def test_run_replay_writes_jsonl_record(tmp_path):
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"fake")
    out = tmp_path / "volatile_shadow.jsonl"

    async def fake_exec(path):
        return [
            json.dumps(_ev(100, "馬文")),
            json.dumps(_ev(600, "馬文播歌")),
            '__DONE__ {"audio_ms": 2500, "wall_ms": 2700}',
        ]

    await volatile_shadow.run_replay(
        str(wav), "狗與露", "馬文播歌", "SwiftV2",
        exec_fn=fake_exec, out_path=out,
    )

    rec = json.loads(out.read_text().strip())
    assert rec["speaker"] == "狗與露"
    assert rec["stable_ms"] == 600
    assert rec["audio_ms"] == 2500
    assert rec["pipeline_text"] == "馬文播歌"
    assert rec["error"] is None
    assert not wav.exists(), "重播後必須刪掉暫存 WAV 副本（不留存使用者音訊）"


@pytest.mark.asyncio
async def test_run_replay_failure_records_error_and_cleans_up(tmp_path):
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"fake")
    out = tmp_path / "volatile_shadow.jsonl"

    async def boom(path):
        raise RuntimeError("bin crashed")

    await volatile_shadow.run_replay(
        str(wav), "狗與露", "x", "Swift", exec_fn=boom, out_path=out,
    )

    rec = json.loads(out.read_text().strip())
    assert "bin crashed" in rec["error"]
    assert not wav.exists()


# ── 閘控 ──────────────────────────────────────────────────────────────────

def test_shadow_disabled_by_default(monkeypatch):
    monkeypatch.delenv("VOLATILE_SHADOW", raising=False)
    assert volatile_shadow.shadow_enabled() is False


def test_sample_rate_gate(monkeypatch):
    monkeypatch.setenv("VOLATILE_SHADOW_RATE", "0.2")
    assert volatile_shadow.should_sample(rng=lambda: 0.1) is True
    assert volatile_shadow.should_sample(rng=lambda: 0.5) is False

"""第 1 刀接線：口白 id 從各條播出路徑真的歸屬到開播紀錄。

mutation check 抓到的死角——把呼叫點換回舊的 `_maybe_play_dj_interjection` /
`_splice_owner_voice_clip`，單元測試照樣全綠。這裡用真的 `_stream_loop` /
`_run_tail_dj` / `_play_tail_dj_after_skip` 跑，斷言 song_plays 紀錄或 info/self 上的歸屬。
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import cogs.music_cog_subsystem as subsystem
import dj_narration_log
import test_dj_tail_wiring as tw
from test_audiophile_playback_sequence import _make_cog_with_vc, _run_loop, _song

pytestmark = pytest.mark.asyncio


def _capture_plays(monkeypatch):
    rows = []
    monkeypatch.setattr(subsystem, "log_song_play", lambda r: rows.append(r))
    monkeypatch.setattr(dj_narration_log, "log_song_play", lambda r: rows.append(r))
    monkeypatch.setattr(dj_narration_log, "log_dj_narration", lambda r: None)
    return rows


def _plays(rows):
    return [r for r in rows if r.get("type") == "play"]


async def test_head_interjection_narration_attributed_to_play(tmp_path, monkeypatch):
    rows = _capture_plays(monkeypatch)
    events = []
    cog, _vc = _make_cog_with_vc(events)
    cog._maybe_play_dj_interjection = AsyncMock(return_value="full")
    song = _song(tmp_path, guide=False)

    await _run_loop(cog, song, events, meta={
        "dj": {"text": "開頭口白", "narration_id": "n-head", "mode": "life"},
        "comment": None, "lyrics": None,
    })

    play = _plays(rows)
    assert play, rows
    assert (play[0]["narration_id"], play[0]["mode"]) == ("n-head", "life")


async def test_head_mixed_dj_audio_attributed_to_play(tmp_path, monkeypatch):
    rows = _capture_plays(monkeypatch)
    events = []
    cog, _vc = _make_cog_with_vc(events)
    song = _song(tmp_path, guide=False)
    dj_path = tmp_path / "dj.opus"
    dj_path.write_bytes(b"x")

    await _run_loop(cog, song, events, meta={
        "dj": {"text": "混音口白", "audio_path": str(dj_path), "narration_id": "n-mix", "mode": "news"},
        "comment": None, "lyrics": None,
    })

    play = _plays(rows)
    assert play, rows
    assert (play[0]["narration_id"], play[0]["mode"]) == ("n-mix", "news")


async def test_tail_narration_stamped_on_next_info(monkeypatch):
    _capture_plays(monkeypatch)
    cog = tw._make_cog()
    cur = tw._cur_info(duration=180.0)
    nxt = tw._next_info()
    cog.stream_queue = [nxt]
    cog._prefetch_cache[nxt["url"]] = tw._done_future(
        {"dj": {**tw._dj_meta(), "narration_id": "n-tail", "mode": "interest"}})
    tw._prime(cog, cur)
    cog._maybe_play_dj_interjection = AsyncMock(return_value="full")

    with patch("os.path.exists", return_value=True), \
         patch("asyncio.sleep", new=AsyncMock()):
        await cog._run_tail_dj(cur, time.time() - 170.0)

    assert (nxt.get("_narration_id"), nxt.get("_narration_mode")) == ("n-tail", "interest")


async def test_seamless_skip_late_narration_attaches_to_current_play(monkeypatch):
    rows = _capture_plays(monkeypatch)
    cog = tw._make_cog()
    nxt = tw._next_info()
    cog._current_stream_info = nxt
    cog._current_play_info = nxt
    cog._current_play_id = "p1"
    cog._current_narration = (None, None)
    cog._resolve_tail_dj_meta = AsyncMock(return_value={
        "text": "x", "audio_path": None, "narration_id": "n-late", "mode": "news"})
    cog._maybe_play_dj_interjection = AsyncMock(return_value="full")

    await cog._play_tail_dj_after_skip(nxt)

    assert cog._current_narration == ("n-late", "news")
    assert "_narration_id" not in nxt
    assert any(r.get("type") == "narration_attach" and r.get("play_id") == "p1"
               and r.get("narration_id") == "n-late" for r in rows)


async def test_splice_and_attach_no_attach_when_file_missing(tmp_path):
    cog = tw._make_cog()
    cog._splice_owner_voice_clip = AsyncMock(return_value=str(tmp_path / "missing.opus"))
    cog._attach_narration = MagicMock()

    out = await cog._splice_and_attach("x", {}, {"narration_id": "n"})

    assert out == str(tmp_path / "missing.opus")
    cog._attach_narration.assert_not_called()

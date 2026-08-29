"""marvin_talk — TalkSession / TalkSessionManager 單元測試（mock google_client + TTS）。

契約（見 marvin_talk.py docstring）：
- /marvin_talk toggle：閒置→開（暫停音樂）、進行中→關（恢復音樂）
- 90s 硬上限 + 25s 靜音由 watchdog 自動收
- feed()：只有觸發者的音訊進 Gemini，其他人被 manager 擋掉
- 每回合 guard.allow() 守門 + guard.record() 記帳；超 cap 優雅結束
- 使用者說結束語（heard 含「掰掰馬文」等）→ 主動收會話
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import marvin_talk
from llm_paid import PaidUsageGuard


class _FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _fake_gemini_response(reply="嗯，隨便啦。", heard="你好", in_tok=1200, out_tok=40):
    resp = MagicMock()
    resp.text = json.dumps({"heard": heard, "reply": reply})
    resp.usage_metadata = MagicMock(prompt_token_count=in_tok, candidates_token_count=out_tok)
    return resp


def _fake_client(response=None, exc=None):
    client = MagicMock()
    if exc is not None:
        client.aio.models.generate_content = AsyncMock(side_effect=exc)
    else:
        client.aio.models.generate_content = AsyncMock(
            return_value=response or _fake_gemini_response()
        )
    return client


def _guard(tmp_path, daily=2.0):
    return PaidUsageGuard(
        log_path=tmp_path / "paid.jsonl", daily_cap_usd=daily, monthly_cap_usd=10.0,
        clock=lambda: 1000.0,
    )


def _manager(tmp_path, *, client, clock=None, tts=None, pause=None, resume=None, send=None):
    return marvin_talk.TalkSessionManager(
        google_client_provider=lambda: client,
        play_tts=tts or AsyncMock(),
        send_text=send or AsyncMock(),
        pause_music=pause or MagicMock(),
        resume_music=resume or MagicMock(),
        persona_provider=lambda: "你是馬文。",
        paid_guard=_guard(tmp_path),
        clock=clock or (lambda: 1000.0),
    )


# 一段假 PCM：48k stereo int16，0.5 秒
_PCM = b"\x01\x00\x02\x00" * (48000 // 2)


@pytest.fixture(autouse=True)
def _stub_resample(monkeypatch):
    """繞開 numpy 重採樣，回一段固定長度的假 wav bytes。"""
    monkeypatch.setattr(
        marvin_talk, "_pcm48k_stereo_to_wav16k_mono",
        lambda pcm: b"RIFF" + b"\x00" * (16000 * 2),  # ~1s 音訊
    )


@pytest.mark.asyncio
async def test_toggle_opens_and_closes(tmp_path):
    pause, resume = MagicMock(), MagicMock()
    mgr = _manager(tmp_path, client=_fake_client(), pause=pause, resume=resume)

    msg = await mgr.toggle(1, "Alice")
    assert "Alice" in msg
    assert mgr.active and mgr.owner_id == 1
    pause.assert_called_once()

    msg2 = await mgr.toggle(2, "Bob")  # 任何人都能關
    assert "結束" in msg2
    assert not mgr.active
    resume.assert_called_once()


@pytest.mark.asyncio
async def test_second_user_cannot_open_while_active(tmp_path):
    mgr = _manager(tmp_path, client=_fake_client())
    await mgr.start(1, "Alice")
    msg = await mgr.start(2, "Bob")
    assert "Alice" in msg
    assert mgr.owner_id == 1


@pytest.mark.asyncio
async def test_feed_only_owner_reaches_gemini(tmp_path):
    tts = AsyncMock()
    client = _fake_client()
    mgr = _manager(tmp_path, client=client, tts=tts)
    await mgr.start(1, "Alice")

    await mgr.feed(999, _PCM)  # 別人
    client.aio.models.generate_content.assert_not_called()

    await mgr.feed(1, _PCM)  # 觸發者
    client.aio.models.generate_content.assert_awaited_once()
    # 開場提示語 + 回覆 → 兩次 TTS；最後一次是回覆
    assert tts.await_args.args[0] == "嗯，隨便啦。"


@pytest.mark.asyncio
async def test_turn_records_paid_usage(tmp_path):
    guard = _guard(tmp_path)
    mgr = marvin_talk.TalkSessionManager(
        google_client_provider=lambda: _fake_client(),
        play_tts=AsyncMock(), send_text=AsyncMock(),
        pause_music=MagicMock(), resume_music=MagicMock(),
        persona_provider=lambda: "你是馬文。", paid_guard=guard,
        clock=lambda: 1000.0,
    )
    await mgr.start(1, "Alice")
    await mgr.feed(1, _PCM)

    rows = guard._rows()
    assert len(rows) == 1
    assert rows[0]["caller"] == "marvin_talk"
    assert rows[0]["est_usd"] > 0


@pytest.mark.asyncio
async def test_over_cap_ends_session(tmp_path):
    guard = _guard(tmp_path, daily=0.0)  # 任何花費都超
    tts = AsyncMock()
    mgr = marvin_talk.TalkSessionManager(
        google_client_provider=lambda: _fake_client(),
        play_tts=tts, send_text=AsyncMock(),
        pause_music=MagicMock(), resume_music=MagicMock(),
        persona_provider=lambda: "你是馬文。", paid_guard=guard,
        clock=lambda: 1000.0,
    )
    await mgr.start(1, "Alice")
    await mgr.feed(1, _PCM)
    assert not mgr.active
    assert "額度" in tts.await_args.args[0]


@pytest.mark.asyncio
async def test_exit_phrase_closes_session(tmp_path):
    client = _fake_client(_fake_gemini_response(reply="好啦掰。", heard="好了掰掰馬文"))
    resume = MagicMock()
    mgr = _manager(tmp_path, client=client, resume=resume)
    await mgr.start(1, "Alice")
    await mgr.feed(1, _PCM)
    assert not mgr.active
    resume.assert_called_once()


@pytest.mark.asyncio
async def test_model_chain_falls_back_on_first_model_error(tmp_path):
    tts = AsyncMock()
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(
        side_effect=[RuntimeError("503 high demand"), _fake_gemini_response(reply="好啦。")]
    )
    mgr = _manager(tmp_path, client=client, tts=tts)
    await mgr.start(1, "Alice")
    await mgr.feed(1, _PCM)

    assert client.aio.models.generate_content.await_count == 2  # 第一家掛 → 換第二家
    assert tts.await_args.args[0] == "好啦。"
    assert mgr.active


@pytest.mark.asyncio
async def test_gemini_failure_keeps_session_and_apologizes(tmp_path):
    tts = AsyncMock()
    mgr = _manager(tmp_path, client=_fake_client(exc=RuntimeError("boom")), tts=tts)
    await mgr.start(1, "Alice")
    await mgr.feed(1, _PCM)
    assert mgr.active  # 單次失敗不收會話
    assert "再說一次" in tts.await_args.args[0]


@pytest.mark.asyncio
async def test_no_wall_clock_cap_only_idle(tmp_path):
    clock = _FakeClock()
    mgr = _manager(tmp_path, client=_fake_client(), clock=clock)
    await mgr.start(1, "Alice")
    # 講過很多輪、掛了很久，只要一直在講話就不會被收
    for _ in range(20):
        clock.t += marvin_talk.IDLE_TIMEOUT_S - 10
        mgr.session._last_activity = clock.t
        assert mgr.session.deadline_reason() is None
    # 停止講話 → 閒置逾時才收
    clock.t += marvin_talk.IDLE_TIMEOUT_S + 1
    assert mgr.session.deadline_reason() == "太久沒聲音"


@pytest.mark.asyncio
async def test_watchdog_ends_on_voice_disconnect(tmp_path, monkeypatch):
    connected = {"v": True}
    mgr = marvin_talk.TalkSessionManager(
        google_client_provider=lambda: _fake_client(),
        play_tts=AsyncMock(), send_text=AsyncMock(),
        pause_music=MagicMock(), resume_music=MagicMock(),
        persona_provider=lambda: "你是馬文。", paid_guard=_guard(tmp_path),
        is_voice_connected=lambda: connected["v"], clock=lambda: 1000.0,
    )
    await mgr.start(1, "Alice")
    mgr._watchdog.cancel()  # 停掉自動 watchdog，手動跑一輪

    async def _sleep_then_break(_):
        raise asyncio.CancelledError  # 讓 _watch 跑完第一輪就退出
    monkeypatch.setattr(marvin_talk.asyncio, "sleep", _sleep_then_break)

    connected["v"] = False
    await mgr._watch()
    assert not mgr.active


@pytest.mark.asyncio
async def test_no_google_client_refuses(tmp_path):
    mgr = _manager(tmp_path, client=None)
    msg = await mgr.start(1, "Alice")
    assert "沒接上" in msg
    assert not mgr.active

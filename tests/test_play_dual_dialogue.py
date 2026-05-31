"""VoiceController.play_dual_dialogue — Marmo 一搭一唱 PoC 雙段播放。

驗證：
  - segments 為 None / 空 → no-op，不呼叫 play_tts
  - happy path：2 segments → 2 次 play_tts，順序 [marvin → marmo]，中間 await sleep
  - voice mapping：marvin 段 voice=None（用 play_tts 預設 Marvin 聲），marmo 段帶 MARMO_VOICE
  - 空 text 的 segment 跳過
  - play_tts 拋例外 → bail，不繼續播下一段（避免半個 dual 卡台）
  - 1 segment（degenerate）→ play 1 次、不 sleep

play_tts 自己內部已 acquire playback_lock，所以這裡不再包外層 lock（會 re-entrant deadlock）。
段間有 ~ms 級 race window 可能被音樂插進來——PoC 接受，Phase 2 視需要再優化。
"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock, patch

import pytest

from cogs.voice_controller import VoiceController


def _fake_vc():
    """Minimal vc-shaped object — play_dual_dialogue only touches play_tts."""
    fake = types.SimpleNamespace()
    fake.play_tts = AsyncMock(return_value=None)
    return fake


# ── No-op cases ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_segments_no_op():
    fake = _fake_vc()
    await VoiceController.play_dual_dialogue(fake, [])
    fake.play_tts.assert_not_called()


@pytest.mark.asyncio
async def test_none_segments_no_op():
    fake = _fake_vc()
    await VoiceController.play_dual_dialogue(fake, None)
    fake.play_tts.assert_not_called()


# ── Happy path ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_two_segments_play_in_order():
    fake = _fake_vc()
    segments = [
        {"voice": "marvin", "text": "時間。又是時間。"},
        {"voice": "marmo", "text": "閉嘴，下午三點四十二。"},
    ]
    await VoiceController.play_dual_dialogue(fake, segments)
    assert fake.play_tts.await_count == 2
    # 第一次：Marvin 段
    args0, kwargs0 = fake.play_tts.call_args_list[0]
    assert args0[0] == "時間。又是時間。"
    # Marvin 用預設聲 → voice=None
    assert kwargs0.get("voice") is None
    # 第二次：Marmo 段，voice 帶 MARMO_VOICE
    args1, kwargs1 = fake.play_tts.call_args_list[1]
    assert args1[0] == "閉嘴，下午三點四十二。"
    assert kwargs1.get("voice")  # 不為 None / 不為空


@pytest.mark.asyncio
async def test_marmo_voice_from_env(monkeypatch):
    monkeypatch.setenv("MARMO_VOICE", "zh-TW-TestVoice")
    fake = _fake_vc()
    await VoiceController.play_dual_dialogue(fake, [
        {"voice": "marvin", "text": "a"},
        {"voice": "marmo", "text": "b"},
    ])
    _, kwargs1 = fake.play_tts.call_args_list[1]
    assert kwargs1["voice"] == "zh-TW-TestVoice"


@pytest.mark.asyncio
async def test_already_in_channel_true_for_both_segments():
    """雙段都該帶 already_in_channel=True（marmo_server 觸發、bot 已在頻道）。"""
    fake = _fake_vc()
    await VoiceController.play_dual_dialogue(fake, [
        {"voice": "marvin", "text": "a"},
        {"voice": "marmo", "text": "b"},
    ])
    for call in fake.play_tts.call_args_list:
        assert call.kwargs.get("already_in_channel") is True


@pytest.mark.asyncio
async def test_pause_between_segments():
    """段間有短停頓（asyncio.sleep）；2 段 → sleep 1 次（最後一段不 sleep）。"""
    fake = _fake_vc()
    with patch("cogs.voice_controller.asyncio.sleep", new=AsyncMock()) as sleep_mock:
        await VoiceController.play_dual_dialogue(fake, [
            {"voice": "marvin", "text": "a"},
            {"voice": "marmo", "text": "b"},
        ])
    # 2 段 → 1 次 sleep；sleep 時長介於 0.1~0.6 秒
    assert sleep_mock.await_count == 1
    pause_dur = sleep_mock.await_args.args[0]
    assert 0.1 <= pause_dur <= 0.6


# ── Edge cases ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_text_segment_skipped():
    fake = _fake_vc()
    await VoiceController.play_dual_dialogue(fake, [
        {"voice": "marvin", "text": "   "},  # 空 text
        {"voice": "marmo", "text": "閉嘴。"},
    ])
    # 只有 marmo 那段被播
    assert fake.play_tts.await_count == 1
    args, _ = fake.play_tts.call_args
    assert args[0] == "閉嘴。"


@pytest.mark.asyncio
async def test_play_tts_raises_bail_no_next_segment():
    fake = _fake_vc()
    fake.play_tts = AsyncMock(side_effect=RuntimeError("voice client disconnected"))
    await VoiceController.play_dual_dialogue(fake, [
        {"voice": "marvin", "text": "a"},
        {"voice": "marmo", "text": "b"},
    ])
    # Marvin call 出錯就 bail，Marmo 不該被呼叫
    assert fake.play_tts.await_count == 1


@pytest.mark.asyncio
async def test_single_segment_no_pause():
    """1 個 segment（degenerate case）→ 播 1 次，不 sleep（最後一段不停頓）。"""
    fake = _fake_vc()
    with patch("cogs.voice_controller.asyncio.sleep", new=AsyncMock()) as sleep_mock:
        await VoiceController.play_dual_dialogue(fake, [
            {"voice": "marvin", "text": "a"},
        ])
    assert fake.play_tts.await_count == 1
    sleep_mock.assert_not_called()

"""
TDD：Layer 2 VAD V11 — sink.write 在 audio 處理異常時不該誤上報 Sentinel。

問題：底層 except 區塊有條 dead branch：
  if ("invalid" in error_msg or "lost" in error_msg) and self.sink_error_callback:
      pass
condition 計算了卻 body=pass，留著誤導未來維護者以為「會在某情況上報」。

設計意圖：partial/lost packet（opus decode 失敗等）刻意**不**上報 Sentinel，
因為這些通常是網路抖動，不是 DAVE 金鑰失效；上報會污染 Sentinel 計數器
誤觸發 soft_repair。Sentinel 只應該被真正的 DAVE 解密失敗觸發
（已在上面 DAVE handling 區塊內處理）。

修法：刪掉 dead `if/pass`，把設計意圖留在註解。
這條 test 同時做為「未來不該回頭把 except 區塊改成上報 Sentinel」的 guard。
"""
from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock, patch

import pytest


class MockUser:
    def __init__(self, user_id):
        self.id = user_id
        self.name = f"TestUser_{user_id}"


class MockVoiceData:
    def __init__(self, opus_bytes=b"dummy"):
        self.opus = opus_bytes


class LoopStub:
    def __init__(self):
        self.tasks = []

    def create_task(self, coro):
        try:
            task = asyncio.create_task(coro)
            self.tasks.append(task)
            return task
        except RuntimeError:
            # 沒有 running loop 時直接吞掉，反正測試不需要真的跑
            try:
                coro.close()
            except Exception:
                pass
            return None


@pytest.mark.asyncio
async def test_opus_decode_invalid_does_not_call_sink_error_callback():
    """opus decode 拋出含 'invalid' 字眼的 exception → sink_error_callback 不該被呼叫。"""
    from discord_voice_engine import RealtimeVADSink

    sink_error_cb = MagicMock()

    async def _async_cut(*args, **kwargs):
        pass

    with patch("discord.ext.voice_recv.AudioSink.__init__", return_value=None):
        s = RealtimeVADSink(
            on_speech_cut_callback=_async_cut,
            sink_error_callback=sink_error_cb,
        )
        s._voice_client = MagicMock()
        s._voice_client._connection = MagicMock()
        s._voice_client._connection.dave_session = None
        s.loop = LoopStub()

        user = MockUser(99)
        data = MockVoiceData(opus_bytes=b"dummy")

        # 讓 opus decode 直接拋 "invalid packet" exception
        with patch("discord.opus.Decoder") as MockDecoder:
            decoder = MockDecoder.return_value
            decoder.decode.side_effect = Exception("invalid packet length")
            s.write(user, data)

        sink_error_cb.assert_not_called()


@pytest.mark.asyncio
async def test_opus_decode_lost_does_not_call_sink_error_callback():
    """opus decode 拋出含 'lost' 字眼的 exception → sink_error_callback 不該被呼叫。"""
    from discord_voice_engine import RealtimeVADSink

    sink_error_cb = MagicMock()

    async def _async_cut(*args, **kwargs):
        pass

    with patch("discord.ext.voice_recv.AudioSink.__init__", return_value=None):
        s = RealtimeVADSink(
            on_speech_cut_callback=_async_cut,
            sink_error_callback=sink_error_cb,
        )
        s._voice_client = MagicMock()
        s._voice_client._connection = MagicMock()
        s._voice_client._connection.dave_session = None
        s.loop = LoopStub()

        user = MockUser(99)
        data = MockVoiceData(opus_bytes=b"dummy")

        with patch("discord.opus.Decoder") as MockDecoder:
            decoder = MockDecoder.return_value
            decoder.decode.side_effect = Exception("packet was lost in transit")
            s.write(user, data)

        sink_error_cb.assert_not_called()


@pytest.mark.asyncio
async def test_generic_exception_does_not_call_sink_error_callback():
    """其他底層 exception（非 DAVE crypto）也不該觸發 Sentinel。"""
    from discord_voice_engine import RealtimeVADSink

    sink_error_cb = MagicMock()

    async def _async_cut(*args, **kwargs):
        pass

    with patch("discord.ext.voice_recv.AudioSink.__init__", return_value=None):
        s = RealtimeVADSink(
            on_speech_cut_callback=_async_cut,
            sink_error_callback=sink_error_cb,
        )
        s._voice_client = MagicMock()
        s._voice_client._connection = MagicMock()
        s._voice_client._connection.dave_session = None
        s.loop = LoopStub()

        user = MockUser(99)
        data = MockVoiceData(opus_bytes=b"dummy")

        with patch("discord.opus.Decoder") as MockDecoder:
            decoder = MockDecoder.return_value
            decoder.decode.side_effect = RuntimeError("unexpected decoder state")
            s.write(user, data)

        sink_error_cb.assert_not_called()

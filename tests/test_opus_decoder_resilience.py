"""
TDD：Opus 解碼器自我修復與異常隔離測試 (針對 silk/resampler.c SIGABRT 崩潰問題)

當 DAVE/SRTP 網路封包發生損壞或長度異常時，Libopus 的 C 內部 state 容易腐敗。
如果繼續重用同一個 discord.opus.Decoder 實例，下次解碼會觸發 silk/resampler.c 的 assertion failure，
導致 SIGABRT (Signal 6) 物理崩潰整個 Python 進程。

修復要求：
1. 長度小於 2 bytes 的無效 Opus 封包直接跳過，不送入 解碼器。
2. 解碼過程拋出 Exception 時，必須將該 user_id 從 self.decoders 中剔除 (pop)，
   確保下一封包會重新建立乾淨的 Decoder 實例，而不是重用已腐敗的 C 狀態。
"""
import asyncio
from unittest.mock import MagicMock, patch
import pytest

class MockUser:
    def __init__(self, user_id):
        self.id = user_id
        self.name = f"User_{user_id}"

class MockVoiceData:
    def __init__(self, opus_bytes=b"valid_opus_bytes"):
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
            try:
                coro.close()
            except Exception:
                pass
            return None

@pytest.mark.asyncio
async def test_engine_sink_evicts_decoder_on_exception():
    from discord_voice_engine import RealtimeVADSink

    async def _async_cut(*args, **kwargs):
        pass

    with patch("discord.ext.voice_recv.AudioSink.__init__", return_value=None):
        sink = RealtimeVADSink(on_speech_cut_callback=_async_cut)
        sink._voice_client = MagicMock()
        sink._voice_client._connection = MagicMock()
        sink._voice_client._connection.dave_session = None
        sink.loop = LoopStub()

        user = MockUser(123)
        data = MockVoiceData(b"bad_opus_payload")

        mock_decoder = MagicMock()
        mock_decoder.decode.side_effect = Exception("corrupted opus silk frame")

        with patch("discord.opus.Decoder", return_value=mock_decoder):
            sink.write(user, data)

        # 解碼失敗後，self.decoders 必須不包含 123
        assert 123 not in sink.decoders

@pytest.mark.asyncio
async def test_core_sink_evicts_decoder_on_exception():
    from marvin_voice_core.sink import RealtimeVADSink

    async def _async_cut(*args, **kwargs):
        pass

    with patch("discord.ext.voice_recv.AudioSink.__init__", return_value=None):
        sink = RealtimeVADSink(on_speech_cut_callback=_async_cut)
        sink._voice_client = MagicMock()
        sink._voice_client._connection = MagicMock()
        sink._voice_client._connection.dave_session = None
        sink.loop = LoopStub()

        user = MockUser(456)
        data = MockVoiceData(b"bad_opus_payload")

        mock_decoder = MagicMock()
        mock_decoder.decode.side_effect = Exception("corrupted opus silk frame")

        with patch("discord.opus.Decoder", return_value=mock_decoder):
            sink.write(user, data)

        # 解碼失敗後，self.decoders 必須不包含 456
        assert 456 not in sink.decoders

@pytest.mark.asyncio
async def test_engine_sink_skips_short_opus_packet():
    from discord_voice_engine import RealtimeVADSink

    async def _async_cut(*args, **kwargs):
        pass

    with patch("discord.ext.voice_recv.AudioSink.__init__", return_value=None):
        sink = RealtimeVADSink(on_speech_cut_callback=_async_cut)
        sink._voice_client = MagicMock()
        sink._voice_client._connection = MagicMock()
        sink._voice_client._connection.dave_session = None
        sink.loop = LoopStub()

        user = MockUser(789)
        data = MockVoiceData(b"\x00")  # 太短 (1 byte)

        mock_decoder = MagicMock()

        with patch("discord.opus.Decoder", return_value=mock_decoder):
            sink.write(user, data)

        # 不應該呼叫 decode
        mock_decoder.decode.assert_not_called()

@pytest.mark.asyncio
async def test_core_sink_skips_short_opus_packet():
    from marvin_voice_core.sink import RealtimeVADSink

    async def _async_cut(*args, **kwargs):
        pass

    with patch("discord.ext.voice_recv.AudioSink.__init__", return_value=None):
        sink = RealtimeVADSink(on_speech_cut_callback=_async_cut)
        sink._voice_client = MagicMock()
        sink._voice_client._connection = MagicMock()
        sink._voice_client._connection.dave_session = None
        sink.loop = LoopStub()

        user = MockUser(999)
        data = MockVoiceData(b"\x00")  # 太短 (1 byte)

        mock_decoder = MagicMock()

        with patch("discord.opus.Decoder", return_value=mock_decoder):
            sink.write(user, data)

        # 不應該呼叫 decode
        mock_decoder.decode.assert_not_called()

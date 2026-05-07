import pytest
import asyncio
from unittest.mock import MagicMock, patch
import time
import numpy as np
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from discord_voice_engine import RealtimeVADSink

class MockUser:
    def __init__(self, user_id):
        self.id = user_id
        self.name = f"TestUser_{user_id}"

class MockVoiceData:
    def __init__(self, opus_bytes=b"dummy"):
        self.opus = opus_bytes

def generate_pcm_frame(rms_value, duration_ms=20):
    num_samples = int(48000 * duration_ms / 1000) * 2
    arr = np.full(num_samples, rms_value, dtype=np.int16)
    arr[1::2] = -rms_value
    return arr.tobytes()

class TimeMock:
    def __init__(self):
        self.t = 100000.0
    def __call__(self):
        self.t += 0.02
        return self.t

class LoopStub:
    def __init__(self):
        self.tasks = []

    def create_task(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task

@pytest.fixture
def sink():
    async def async_on_cut(*args, **kwargs):
        pass
    
    on_start = MagicMock()
    
    with patch('discord.ext.voice_recv.AudioSink.__init__', return_value=None):
        s = RealtimeVADSink(on_speech_cut_callback=async_on_cut, on_speech_start_callback=on_start)
        s._voice_client = MagicMock()
        s._voice_client._connection = MagicMock()
        s._voice_client._connection.dave_session = None
        loop_stub = LoopStub()
        s.loop = loop_stub
        yield s, on_start
        for task in loop_stub.tasks:
            if not task.done():
                task.cancel()

@pytest.mark.asyncio
async def test_pure_noise_ignored(sink):
    s, on_start = sink
    user = MockUser(1)
    
    tm = TimeMock()
    
    with patch('discord.opus.Decoder') as MockDecoder, patch('time.time', side_effect=tm):
        decoder_instance = MockDecoder.return_value
        decoder_instance.decode.return_value = generate_pcm_frame(250)
        
        # 5 seconds = 250 frames of 20ms
        for i in range(250):
            s.write(user, MockVoiceData())
            if i == 0:
                # Initial sudden burst of 250 RMS when floor is 50 will trigger VAD.
                # This is expected. We reset the mock to verify it doesn't trigger AGAIN once adapted.
                on_start.reset_mock()
            
        # Assert no SUBSEQUENT interrupt was triggered
        on_start.assert_not_called()
        
        # Assert noise floor adapted to ~250
        assert s.user_noise_floor[1] >= 240

@pytest.mark.asyncio
async def test_burst_speech_triggers(sink):
    s, on_start = sink
    user = MockUser(2)
    
    tm = TimeMock()
    
    with patch('discord.opus.Decoder') as MockDecoder, patch('time.time', side_effect=tm):
        decoder_instance = MockDecoder.return_value
        
        # 3 seconds of 250 RMS noise
        decoder_instance.decode.return_value = generate_pcm_frame(250)
        for i in range(150):
            s.write(user, MockVoiceData())
            if i == 0:
                on_start.reset_mock()
            
        on_start.assert_not_called()
        
        # Burst speech at 800 RMS
        decoder_instance.decode.return_value = generate_pcm_frame(800)
        for _ in range(5):
            s.write(user, MockVoiceData())
            
        # Should be triggered now
        on_start.assert_called_once_with(2)

@pytest.mark.asyncio
async def test_quick_recovery(sink):
    s, on_start = sink
    user = MockUser(3)
    
    tm = TimeMock()
    
    with patch('discord.opus.Decoder') as MockDecoder, patch('time.time', side_effect=tm):
        decoder_instance = MockDecoder.return_value
        
        # 2 seconds of 800 RMS continuous noise
        decoder_instance.decode.return_value = generate_pcm_frame(800)
        for _ in range(100):
            s.write(user, MockVoiceData())
            
        assert on_start.called
        on_start.reset_mock()
        
        # Wait a bit for event-driven silence to flush
        # 800 RMS noise stops, drop to 5 RMS for 1 second.
        decoder_instance.decode.return_value = generate_pcm_frame(5)
        for _ in range(50):
            s.write(user, MockVoiceData())
            
        # Deadlock recovery should kick in immediately.
        # Plus, event-driven silence should have reset user_is_speaking to False.
        assert s.user_noise_floor[3] <= 15
        assert s.user_is_speaking[3] is False
        
        # Burst at 160 RMS
        decoder_instance.decode.return_value = generate_pcm_frame(160)
        for _ in range(5):
            s.write(user, MockVoiceData())
            
        # Should trigger again!
        on_start.assert_called_once_with(3)

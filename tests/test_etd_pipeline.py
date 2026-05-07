import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock

# Mock the dependencies
class MockVoiceController:
    def __init__(self):
        self.user_sentence_buffer = {}
        self.speech_timers = {}
        self.speech_buffers = {}
        self.bot = MagicMock()
        self.bot.router = MagicMock()
        self.bot.router.clean_stt_text = AsyncMock()
        self.pending_mock_users = set()
        self.process_debounced_speech_mock = MagicMock()

    async def process_debounced_speech(self, speaker):
        self.process_debounced_speech_mock(speaker)

    async def handle_stt_result(self, speaker: str, raw_text: str, timestamp: float, wav_bytes: bytes, prosody_data: dict = None, is_wake_check=False, track=None, bypass_etd=False):
        # 簡單複製 voice_controller.py 內的邏輯，測試 Semantic ETD
        import time
        self.last_player_speech_time = time.time()
        self.proactive_attempts = 0
        self._stt_call_counter = getattr(self, "_stt_call_counter", 0) + 1

        if not is_wake_check and not bypass_etd:
            import re
            
            buf = self.user_sentence_buffer.get(speaker, {})
            accumulated = buf.get("texts", [])
            
            if buf.get("task") and not buf["task"].done():
                buf["task"].cancel()
                
            combined_texts = accumulated + [raw_text]
            combined_text = "，".join(combined_texts)
            origin_ts = buf.get("timestamp", timestamp)
            origin_pd = buf.get("prosody_data") or prosody_data
            
            is_complete = True
            heuristic_triggered = False
            
            thinking_words_re = re.compile(r'(然後|就是|那個|我覺得|如果|所以|因為|但是|可能|的話|還是|或者)[.。…\s]*$', re.IGNORECASE)
            if thinking_words_re.search(combined_text):
                is_complete = False
                heuristic_triggered = True
            elif not re.search(r'[。！？.!?]\s*$', combined_text) and len(combined_texts) < 5:
                pass
            
            if is_complete and not heuristic_triggered:
                if hasattr(self.bot, "router") and hasattr(self.bot.router, "clean_stt_text"):
                    res = await self.bot.router.clean_stt_text(combined_text)
                    if isinstance(res, dict) and "is_complete" in res:
                        is_complete = res["is_complete"]
            
            if not is_complete and len(combined_texts) < 5:
                async def _flush(spk=speaker, texts=combined_texts, ts=origin_ts, pd=origin_pd, wb=wav_bytes, t=track):
                    await asyncio.sleep(0.5) # 用 0.5s 代替 2.5s 縮短測試時間
                    self.user_sentence_buffer.pop(spk, None)
                    joined = "，".join(texts)
                    await self.handle_stt_result(spk, joined, ts, wb, prosody_data=pd, is_wake_check=False, track=t, bypass_etd=True)

                task = asyncio.create_task(_flush())
                self.user_sentence_buffer[speaker] = {"texts": combined_texts, "task": task, "timestamp": origin_ts, "prosody_data": origin_pd}
                return
            else:
                self.user_sentence_buffer.pop(speaker, None)
                raw_text = combined_text
                timestamp = origin_ts

        # Append to buffer
        if speaker not in self.speech_buffers:
            self.speech_buffers[speaker] = {"texts": [], "first_timestamp": timestamp, "wav_bytes": bytearray()}
        if not is_wake_check:
            self.speech_buffers[speaker]["texts"].append(raw_text)
            
        asyncio.create_task(self.process_debounced_speech(speaker))


@pytest.mark.asyncio
async def test_etd_heuristic():
    """Test Case 1 (Heuristic 攔截): 模擬包含思考詞的結尾，應在本地被攔截並等待"""
    controller = MockVoiceController()
    
    # 這裡的文字缺少句號且包含「那個」
    await controller.handle_stt_result("User1", "我昨天去看了那個", 100.0, b"")
    
    # Assert
    assert "User1" in controller.user_sentence_buffer
    assert controller.bot.router.clean_stt_text.call_count == 0 # 不該呼叫外部 API
    assert controller.process_debounced_speech_mock.call_count == 0 # 還沒結算
    
    # Mock user finishes the thought within timer
    await controller.handle_stt_result("User1", "電影，很好看。", 101.0, b"")
    
    # Wait a bit for tasks to complete
    await asyncio.sleep(0.1)
    assert controller.process_debounced_speech_mock.call_count == 1
    assert "User1" not in controller.user_sentence_buffer
    assert controller.speech_buffers["User1"]["texts"][-1] == "我昨天去看了那個，電影，很好看。"


@pytest.mark.asyncio
async def test_etd_groq_semantic():
    """Test Case 2 (Groq 語意判定): 本地正則無法判定，交由 LLM 判斷未完成"""
    controller = MockVoiceController()
    
    # 這裡沒有典型思考詞，也沒有標點，進入 Groq
    async def mock_clean(*args, **kwargs):
        return {"text": "可是如果我們把這個參數調高", "is_complete": False}
    controller.bot.router.clean_stt_text.side_effect = mock_clean
    
    await controller.handle_stt_result("User1", "可是如果我們把這個參數調高", 100.0, b"")
    
    # Assert
    assert "User1" in controller.user_sentence_buffer
    assert controller.bot.router.clean_stt_text.call_count == 1 # 呼叫了外部 API
    assert controller.process_debounced_speech_mock.call_count == 0 # 還沒結算


@pytest.mark.asyncio
async def test_etd_hard_threshold():
    """Test Case 3 (Hard Threshold): 超時後強制結算"""
    controller = MockVoiceController()
    
    await controller.handle_stt_result("User1", "然後...", 100.0, b"")
    
    # Verify it is waiting
    assert "User1" in controller.user_sentence_buffer
    assert controller.process_debounced_speech_mock.call_count == 0
    
    # Wait for the hard threshold (mocked to 0.5s in the test)
    await asyncio.sleep(0.6)
    
    # Verify it was flushed automatically!
    assert "User1" not in controller.user_sentence_buffer
    assert controller.process_debounced_speech_mock.call_count == 1
    assert controller.speech_buffers["User1"]["texts"][-1] == "然後..."

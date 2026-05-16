"""
TDD: twitch_stt_listener 開台前輪詢重試
"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch, call

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


class TestWaitForStream(unittest.TestCase):

    def test_returns_url_immediately_when_live(self):
        import twitch_stt_listener as m
        async def _run():
            with patch.object(m, "get_stream_url", new=AsyncMock(return_value="https://hls.example.com/live.m3u8")):
                url = await m.wait_for_stream("pinpinponpon627", poll_interval=1)
            return url
        url = asyncio.get_event_loop().run_until_complete(_run())
        self.assertEqual(url, "https://hls.example.com/live.m3u8")

    def test_retries_until_stream_starts(self):
        """None, None, url → 應該第三次才回傳"""
        import twitch_stt_listener as m
        calls = []
        async def fake_get_url(channel):
            calls.append(channel)
            return None if len(calls) < 3 else "https://hls.example.com/live.m3u8"

        async def _run():
            with patch.object(m, "get_stream_url", new=fake_get_url):
                with patch("asyncio.sleep", new=AsyncMock()):
                    url = await m.wait_for_stream("pinpinponpon627", poll_interval=1)
            return url

        url = asyncio.get_event_loop().run_until_complete(_run())
        self.assertEqual(url, "https://hls.example.com/live.m3u8")
        self.assertEqual(len(calls), 3)

    def test_logs_waiting_message_when_not_live(self):
        import twitch_stt_listener as m
        attempt_count = [0]
        async def fake_get_url(channel):
            attempt_count[0] += 1
            return None if attempt_count[0] < 2 else "https://url"

        async def _run():
            with patch.object(m, "get_stream_url", new=fake_get_url):
                with patch("asyncio.sleep", new=AsyncMock()):
                    with patch.object(m.log, "info") as mock_log:
                        await m.wait_for_stream("pinpinponpon627", poll_interval=60)
                        # 應該 log 過至少一次等待訊息
                        logged = any("未開台" in str(c) or "等待" in str(c)
                                     for c in mock_log.call_args_list)
            return logged

        logged = asyncio.get_event_loop().run_until_complete(_run())
        self.assertTrue(logged)


if __name__ == "__main__":
    unittest.main()

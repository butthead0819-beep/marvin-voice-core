"""
TDD: twitch_stt_listener Groq fallback + stream_session 同時啟動 STT
"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


class TestGroqFallbackInRunStt(unittest.TestCase):
    """run_stt() macOS 回 None → 自動 fallback 到 Groq"""

    def test_returns_macos_result_when_macos_succeeds(self):
        import twitch_stt_listener as m
        with patch.object(m, "_run_macos_stt", return_value="直播開始了"):
            with patch.object(m, "_run_groq_stt") as mock_groq:
                result = m.run_stt(Path("/tmp/fake.wav"))
        self.assertEqual(result, "直播開始了")
        mock_groq.assert_not_called()

    def test_falls_back_to_groq_when_macos_returns_none(self):
        import twitch_stt_listener as m
        with patch.object(m, "_run_macos_stt", return_value=None):
            with patch.object(m, "_run_groq_stt", return_value="訂閱了"):
                result = m.run_stt(Path("/tmp/fake.wav"))
        self.assertEqual(result, "訂閱了")

    def test_returns_none_when_both_fail(self):
        import twitch_stt_listener as m
        with patch.object(m, "_run_macos_stt", return_value=None):
            with patch.object(m, "_run_groq_stt", return_value=None):
                result = m.run_stt(Path("/tmp/fake.wav"))
        self.assertIsNone(result)

    def test_groq_returns_none_when_no_api_key(self):
        """無 GROQ_API_KEY 時 _run_groq_stt 自己回 None，run_stt 最終回 None"""
        import twitch_stt_listener as m
        import os
        original = os.environ.pop("GROQ_API_KEY", None)
        try:
            with patch.object(m, "_run_macos_stt", return_value=None):
                result = m.run_stt(Path("/tmp/fake.wav"))
            self.assertIsNone(result)
        finally:
            if original:
                os.environ["GROQ_API_KEY"] = original


class TestStreamSessionStartsStt(unittest.TestCase):
    """stream_session.py 啟動時同時跑 twitch_stt_listener"""

    def test_run_session_starts_stt_listener(self):
        import stream_session as ss
        self.assertTrue(
            hasattr(ss, "start_stt_listener"),
            "stream_session 應有 start_stt_listener() 函式"
        )

    def test_start_stt_listener_returns_process(self):
        import stream_session as ss
        mock_proc = MagicMock()
        mock_proc.terminate = MagicMock()

        async def _run():
            with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
                proc = await ss.start_stt_listener("pinpinponpon627")
            return proc

        proc = asyncio.get_event_loop().run_until_complete(_run())
        self.assertIsNotNone(proc)

    def test_stt_listener_terminated_after_session(self):
        """session 結束後 STT process 必須被 terminate"""
        import stream_session as ss
        mock_proc = MagicMock()
        mock_proc.terminate = MagicMock()
        mock_proc.wait = AsyncMock()

        async def _run():
            with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
                await ss.run_with_stt("pinpinponpon627", 0.001)  # 極短 session

        asyncio.get_event_loop().run_until_complete(_run())
        mock_proc.terminate.assert_called_once()


if __name__ == "__main__":
    unittest.main()

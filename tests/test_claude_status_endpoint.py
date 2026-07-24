"""
tests/test_claude_status_endpoint.py
TDD：GET /claude_status — HUD 讀 Claude Code session 狀態橋接檔。

比照 tests/test_now_endpoint_cross_process_fallback.py 同一套測試風格。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from claude_sessions_state import save_claude_sessions_state, save_claude_rate_limits


def _make_vc():
    vc = MagicMock()
    vc.handle_stt_result = AsyncMock()
    vc.bot.cogs.get.return_value = None
    return vc


@pytest.mark.asyncio
async def test_claude_status_returns_sessions_and_rate_limits(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    path = str(tmp_path / "claude_sessions_state.json")
    save_claude_sessions_state(sessions=[
        {"session_id": "abc", "project": "Discord-voice-bot", "cwd": "/x",
         "waiting": True, "last_text": "要不要重跑 CI？", "updated_at": 1.0},
    ], path=path)
    save_claude_rate_limits(
        five_hour={"used_percentage": 23.5, "resets_at": 111},
        seven_day={"used_percentage": 41.2, "resets_at": 222}, path=path)

    app = build_text_app(_make_vc(), token=None, claude_sessions_state_path=path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/claude_status")
        assert resp.status == 200
        body = await resp.json()
        assert body["sessions"][0]["waiting"] is True
        assert body["sessions"][0]["last_text"] == "要不要重跑 CI？"
        assert body["rate_limits"]["five_hour"]["used_percentage"] == 23.5


@pytest.mark.asyncio
async def test_claude_status_returns_empty_when_bridge_file_missing(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    path = str(tmp_path / "claude_sessions_state.json")  # 不存在
    app = build_text_app(_make_vc(), token=None, claude_sessions_state_path=path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/claude_status")
        assert resp.status == 200
        body = await resp.json()
        assert body == {"sessions": [], "rate_limits": None}

"""
tests/test_gmail_calendar_status_endpoint.py
TDD：GET /gmail_calendar_status — HUD 讀 Gmail/Calendar count-only 橋接檔。

比照 tests/test_claude_status_endpoint.py 同一套測試風格。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from gmail_calendar_state import save_gmail_calendar_state


def _make_vc():
    vc = MagicMock()
    vc.handle_stt_result = AsyncMock()
    vc.bot.cogs.get.return_value = None
    return vc


@pytest.mark.asyncio
async def test_returns_counts_when_fresh(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    import time
    path = str(tmp_path / "gmail_calendar_state.json")
    save_gmail_calendar_state(gmail_unread=12, calendar_today_count=2, updated_at=time.time(), path=path)

    app = build_text_app(_make_vc(), token=None, gmail_calendar_state_path=path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/gmail_calendar_status")
        assert resp.status == 200
        body = await resp.json()
        assert body == {"gmail_unread": 12, "calendar_today_count": 2}


@pytest.mark.asyncio
async def test_returns_none_when_stale(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    path = str(tmp_path / "gmail_calendar_state.json")
    save_gmail_calendar_state(gmail_unread=12, calendar_today_count=2, updated_at=0.0, path=path)

    app = build_text_app(_make_vc(), token=None, gmail_calendar_state_path=path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/gmail_calendar_status")
        assert resp.status == 200
        body = await resp.json()
        assert body == {"gmail_unread": None, "calendar_today_count": None}


@pytest.mark.asyncio
async def test_returns_none_when_bridge_file_missing(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    path = str(tmp_path / "does_not_exist.json")

    app = build_text_app(_make_vc(), token=None, gmail_calendar_state_path=path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/gmail_calendar_status")
        assert resp.status == 200
        body = await resp.json()
        assert body == {"gmail_unread": None, "calendar_today_count": None}

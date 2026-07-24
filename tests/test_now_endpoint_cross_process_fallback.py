"""
tests/test_now_endpoint_cross_process_fallback.py
TDD：GET /now 跨進程橋接。

HUD 只在家用，要跟 Pi satellite（main_discord.py 真正在 Discord 播歌那個進程寫的
now_playing_state.json）連動；satellite 進程自己的本地 MusicCog（car puck／瀏覽器
satellite 在外模式播歌用）一律不看，避免在外播放蓋掉家裡 HUD 的畫面。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from now_playing_state import save_now_playing_state


def _make_vc(mc=None):
    vc = MagicMock()
    vc.handle_stt_result = AsyncMock()
    vc.bot.cogs.get.return_value = mc
    return vc


@pytest.mark.asyncio
async def test_now_reads_bridge_file_when_local_cog_idle(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    path = str(tmp_path / "now_playing_state.json")
    save_now_playing_state(playing=True, title="夜曲", by="大肚",
                            cover="http://x/y.jpg", palette=["#111111"],
                            queue=[{"title": "晴天", "by": "小明"}], path=path)
    app = build_text_app(_make_vc(mc=None), token=None, now_playing_state_path=path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/now")
        assert resp.status == 200
        body = await resp.json()
        assert body["playing"] is True
        assert body["title"] == "夜曲"
        assert body["by"] == "大肚"
        assert body["palette"] == ["#111111"]
        assert body["queue"] == [{"title": "晴天", "by": "小明"}]


@pytest.mark.asyncio
async def test_now_bridge_includes_duration_start_time_and_comment(tmp_path):
    """HUD 黑膠展開（進度條/DJ 銳評）要靠這三個欄位，跨進程橋接檔路徑要帶到。"""
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    path = str(tmp_path / "now_playing_state.json")
    save_now_playing_state(playing=True, title="夜曲", by="大肚", cover="http://x/y.jpg",
                            palette=["#111111"], queue=[], duration=245.0,
                            song_start_time=1700000000.0, comment="這首不錯。", path=path)
    app = build_text_app(_make_vc(mc=None), token=None, now_playing_state_path=path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/now")
        body = await resp.json()
        assert body["duration"] == 245.0
        assert body["song_start_time"] == 1700000000.0
        assert body["comment"] == "這首不錯。"


@pytest.mark.asyncio
async def test_now_ignores_local_cog_when_bridge_file_idle(tmp_path):
    """本地 MusicCog 在播（car puck／瀏覽器 satellite 在外）不該讓家用 HUD 顯示為在播。"""
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    path = str(tmp_path / "now_playing_state.json")   # 橋接檔不存在＝家裡沒在播
    mc = MagicMock()
    mc.stream_mode = True
    mc._current_stream_info = {"title": "本地正在播", "requested_by": "車上", "duration": 180.0}
    mc.stream_paused = False
    mc.stream_queue = []
    mc._current_stream_start_time = 1700000001.0
    mc._current_stream_comment = "又是這首。"
    app = build_text_app(_make_vc(mc=mc), token=None, now_playing_state_path=path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/now")
        assert (await resp.json()) == {"playing": False}


@pytest.mark.asyncio
async def test_now_returns_false_when_neither_local_nor_bridge_playing(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    path = str(tmp_path / "now_playing_state.json")   # 不存在
    app = build_text_app(_make_vc(mc=None), token=None, now_playing_state_path=path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/now")
        assert (await resp.json()) == {"playing": False}


@pytest.mark.asyncio
async def test_now_prefers_bridge_file_over_local_cog(tmp_path):
    """car puck／瀏覽器 satellite 本地在播歌，但家裡橋接檔才是 HUD 該顯示的真相。"""
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    path = str(tmp_path / "now_playing_state.json")
    save_now_playing_state(playing=True, title="家裡真的在播", by="Pi",
                            cover="", palette=[], path=path)
    mc = MagicMock()
    mc.stream_mode = True
    mc._current_stream_info = {"title": "在外本地播放", "requested_by": "車上"}
    mc.stream_paused = False
    mc.stream_queue = []
    mc._current_stream_start_time = None
    mc._current_stream_comment = None
    app = build_text_app(_make_vc(mc=mc), token=None, now_playing_state_path=path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/now")
        body = await resp.json()
        assert body["title"] == "家裡真的在播"

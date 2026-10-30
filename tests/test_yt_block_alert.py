"""連續秒跳（疑 YouTube 403）→ DM owner 更新 cookies；debounce 避免洗版。"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs.music_cog_yt_alert import YtBlockAlertMixin


class _Cog(YtBlockAlertMixin):
    def __init__(self):
        self.bot = MagicMock()
        self.owner = MagicMock()
        self.owner.send = AsyncMock()
        self.bot.get_user.return_value = self.owner


def test_below_threshold_no_alert():
    c = _Cog()
    assert not c._yt_block_should_alert(0)
    assert not c._yt_block_should_alert(10)


def test_threshold_alerts_once_then_debounced():
    c = _Cog()
    c._yt_block_should_alert(0); c._yt_block_should_alert(5)
    assert c._yt_block_should_alert(10) is True
    assert c._yt_block_should_alert(15) is False      # debounce 內
    assert c._yt_block_should_alert(4000) is False    # 視窗外只剩 1 筆 → 不達門檻


def test_old_failures_expire_from_window():
    c = _Cog()
    c._yt_block_should_alert(0); c._yt_block_should_alert(5)
    assert c._yt_block_should_alert(500) is False


@pytest.mark.asyncio
async def test_note_sends_dm_on_third_failure():
    c = _Cog()
    for t in (0, 5):
        c._note_instant_eof("A", 0.1, now=t)
    c._note_instant_eof("紅豆", 0.1, now=10)
    await asyncio.sleep(0.05)
    c.owner.send.assert_awaited_once()
    assert "cookies" in c.owner.send.await_args.args[0]
    assert "紅豆" in c.owner.send.await_args.args[0]

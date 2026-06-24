"""夜晚回放秀自動貼文 hook 測試（鏡像日記：關台渲染→開台貼+置頂）。

只測狀態機與貼文行為（pending/posted、貼一次、去重、全防禦），不測 discord 格式。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import make_reveal


@pytest.fixture
def state(tmp_path, monkeypatch):
    """把 pending/posted 狀態檔導到 tmp，避免碰 records/。"""
    monkeypatch.setattr(make_reveal, "REVEAL_PENDING", str(tmp_path / "pending.json"))
    monkeypatch.setattr(make_reveal, "REVEAL_POSTED", str(tmp_path / "posted.txt"))
    return tmp_path


def _png(tmp_path):
    from PIL import Image
    p = str(tmp_path / "reveal.png")
    Image.new("RGB", (40, 30), (10, 10, 10)).save(p)
    return p


# ── 狀態檔 round-trip ──────────────────────────────────────────────
def test_pending_roundtrip(state):
    assert make_reveal._reveal_pending() == {}
    make_reveal._set_reveal_pending("2026-06-24 00:52:13", "records/x.png")
    p = make_reveal._reveal_pending()
    assert p["end"] == "2026-06-24 00:52:13" and p["path"] == "records/x.png"
    make_reveal._clear_reveal_pending()
    assert make_reveal._reveal_pending() == {}


def test_posted_roundtrip(state):
    assert make_reveal._last_reveal_posted() == ""
    make_reveal._mark_reveal_posted("2026-06-24 00:52:13")
    assert make_reveal._last_reveal_posted() == "2026-06-24 00:52:13"


# ── maybe_post_reveal ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_post_reveal_no_pending_returns_none(state):
    assert await make_reveal.maybe_post_reveal(MagicMock()) is None


@pytest.mark.asyncio
async def test_post_reveal_posts_pins_marks_clears(state, monkeypatch):
    png = _png(state)
    make_reveal._set_reveal_pending("END1", png)

    channel = MagicMock()
    sent = MagicMock()
    sent.pin = AsyncMock()
    channel.send = AsyncMock(return_value=sent)
    monkeypatch.setattr("diary_comic_poster._find_diary_channel", lambda bot: channel)

    out = await make_reveal.maybe_post_reveal(MagicMock())

    assert out is channel
    channel.send.assert_awaited_once()
    assert channel.send.call_args.kwargs["content"] == make_reveal.REVEAL_CONTENT
    sent.pin.assert_awaited_once()
    assert make_reveal._last_reveal_posted() == "END1"      # 標記已貼
    assert make_reveal._reveal_pending() == {}              # 清掉 pending


@pytest.mark.asyncio
async def test_post_reveal_idempotent_already_posted(state, monkeypatch):
    png = _png(state)
    make_reveal._set_reveal_pending("END1", png)
    make_reveal._mark_reveal_posted("END1")                 # 同場次已貼過
    channel = MagicMock()
    channel.send = AsyncMock()
    monkeypatch.setattr("diary_comic_poster._find_diary_channel", lambda bot: channel)

    assert await make_reveal.maybe_post_reveal(MagicMock()) is None
    channel.send.assert_not_awaited()                       # 不重貼


@pytest.mark.asyncio
async def test_post_reveal_missing_file_clears_pending(state, monkeypatch):
    make_reveal._set_reveal_pending("END1", str(state / "gone.png"))
    monkeypatch.setattr("diary_comic_poster._find_diary_channel",
                        lambda bot: MagicMock())
    assert await make_reveal.maybe_post_reveal(MagicMock()) is None
    assert make_reveal._reveal_pending() == {}              # 檔不在 → 清 pending


@pytest.mark.asyncio
async def test_post_reveal_no_channel_returns_none(state, monkeypatch):
    png = _png(state)
    make_reveal._set_reveal_pending("END1", png)
    monkeypatch.setattr("diary_comic_poster._find_diary_channel", lambda bot: None)
    assert await make_reveal.maybe_post_reveal(MagicMock()) is None
    # 沒貼成功 → pending 保留、未標 posted（下次開台再試）
    assert make_reveal._reveal_pending().get("end") == "END1"
    assert make_reveal._last_reveal_posted() == ""


@pytest.mark.asyncio
async def test_render_reveal_swallows_errors(state, monkeypatch):
    # 渲染炸掉不可往上拋（不影響關台 loop）
    monkeypatch.setattr(make_reveal, "_render_reveal_blocking",
                        MagicMock(side_effect=RuntimeError("boom")))
    await make_reveal.maybe_render_reveal(MagicMock())      # 不應拋

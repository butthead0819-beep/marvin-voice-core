"""
tests/test_audio_rate_limit.py
TDD：/audio per-token 限速（eng review 架構#2）。

funnel 公開後 /audio 在公網上；token 若外洩，狂打 /audio = 每次一趟 pipeline
= 付費 LLM 被灌爆（違反付費鐵則）。per-token 固定視窗限速，超限 → 429。

純邏輯（注入時鐘）+ HTTP 層（aiohttp TestServer）。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock


def _clock():
    now = [0.0]
    return (lambda: now[0]), (lambda dt: now.__setitem__(0, now[0] + dt))


# ── 純限速器 ────────────────────────────────────────────────────────────────
def test_allows_up_to_max_then_blocks():
    from rate_limiter import RateLimiter
    t, _adv = _clock()
    rl = RateLimiter(max_per_window=3, window_s=60.0, time_fn=t)
    assert rl.allow("k") is True    # 1
    assert rl.allow("k") is True    # 2
    assert rl.allow("k") is True    # 3
    assert rl.allow("k") is False   # 4 超限


def test_window_resets():
    from rate_limiter import RateLimiter
    t, adv = _clock()
    rl = RateLimiter(max_per_window=2, window_s=60.0, time_fn=t)
    assert rl.allow("k") is True
    assert rl.allow("k") is True
    assert rl.allow("k") is False
    adv(61.0)                       # 視窗過了
    assert rl.allow("k") is True    # 重新放行


def test_keys_are_independent():
    from rate_limiter import RateLimiter
    t, _adv = _clock()
    rl = RateLimiter(max_per_window=1, window_s=60.0, time_fn=t)
    assert rl.allow("a") is True
    assert rl.allow("b") is True    # 不同 key 各自計數
    assert rl.allow("a") is False


# ── HTTP /audio 429 ─────────────────────────────────────────────────────────
def _make_vc():
    vc = MagicMock()
    vc.handle_stt_result = AsyncMock()
    vc.bot.cogs.get.return_value = None
    return vc


@pytest.mark.asyncio
async def test_audio_over_limit_returns_429():
    """超限的第 3 次（空 body 本會 400）應先被限速攔成 429。"""
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    from rate_limiter import RateLimiter
    rl = RateLimiter(max_per_window=2, window_s=60.0)
    app = build_text_app(_make_vc(), token="s3cret", audio_rate_limiter=rl)
    async with TestClient(TestServer(app)) as client:
        r1 = await client.post("/audio?t=s3cret", data=b"")   # 允許→空 body→400
        r2 = await client.post("/audio?t=s3cret", data=b"")   # 允許→400
        r3 = await client.post("/audio?t=s3cret", data=b"")   # 超限→429（先於 body 檢查）
        assert r1.status == 400
        assert r2.status == 400
        assert r3.status == 429


@pytest.mark.asyncio
async def test_audio_no_limiter_configured_never_429():
    """未接 limiter（None）→ 不限速，維持現狀。"""
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    app = build_text_app(_make_vc(), token="s3cret")   # 無 audio_rate_limiter
    async with TestClient(TestServer(app)) as client:
        for _ in range(5):
            resp = await client.post("/audio?t=s3cret", data=b"")
            assert resp.status == 400   # 空 body，但絕不 429

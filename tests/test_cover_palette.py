"""封面抽色測試（TDD）——給 vinyl splatter 用的主色調色盤。

用注入的 fetch_bytes 餵記憶體內 PIL 圖，不真連網。
"""
import io

import pytest
from PIL import Image

import cover_palette


def _png(pixels_fn, size=64):
    img = Image.new("RGB", (size, size), (0, 0, 0))
    for x in range(size):
        for y in range(size):
            img.putpixel((x, y), pixels_fn(x, y, size))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _fetch(data):
    async def _f(url, timeout_s=6.0):
        return data
    return _f


def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


@pytest.mark.asyncio
async def test_two_block_image_yields_both_hues():
    # 左半紅、右半藍 → 調色盤要同時抓到偏紅與偏藍
    data = _png(lambda x, y, s: (200, 30, 30) if x < s // 2 else (30, 60, 200))
    pal = await cover_palette.extract_palette("http://x/cover.jpg", n=4, fetch_bytes=_fetch(data))
    assert pal and all(p.startswith("#") and len(p) == 7 for p in pal)
    rgbs = [_hex_to_rgb(p) for p in pal]
    assert any(r > g and r > b for r, g, b in rgbs), "應有偏紅"
    assert any(b > r for r, g, b in rgbs), "應有偏藍"


@pytest.mark.asyncio
async def test_prefers_vivid_over_gray():
    # 大面灰 + 一小塊鮮橙 → 鮮橙應被優先選（vivid 勝過頻率）
    def px(x, y, s):
        return (240, 130, 20) if (x < 10 and y < 10) else (128, 128, 128)
    data = _png(px)
    pal = await cover_palette.extract_palette("http://x", n=3, fetch_bytes=_fetch(data))
    rgbs = [_hex_to_rgb(p) for p in pal]
    assert any(r > 180 and 60 < g < 180 and b < 90 for r, g, b in rgbs), "鮮橙應入選"


@pytest.mark.asyncio
async def test_empty_url_returns_empty():
    async def _must_not_call(url, timeout_s=6.0):
        raise AssertionError("空 URL 不該下載")
    assert await cover_palette.extract_palette("", fetch_bytes=_must_not_call) == []


@pytest.mark.asyncio
async def test_fetch_failure_returns_empty():
    async def _none(url, timeout_s=6.0):
        return None
    assert await cover_palette.extract_palette("http://x", fetch_bytes=_none) == []


@pytest.mark.asyncio
async def test_respects_n_and_hex_format():
    data = _png(lambda x, y, s: ((x * 4) % 256, (y * 4) % 256, ((x + y) * 2) % 256))
    pal = await cover_palette.extract_palette("http://x", n=3, fetch_bytes=_fetch(data))
    assert len(pal) <= 3
    for p in pal:
        assert len(p) == 7 and p[0] == "#"
        int(p[1:], 16)  # 合法 hex

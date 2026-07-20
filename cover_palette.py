"""從專輯封面抽主色調色盤（給 vinyl splatter 用）。純 PIL，免加依賴。

Why：vinyl 潑漆要「每張專輯不同」＝從封面本身抽鮮豔主色。
策略：縮圖 → median-cut 量化 → 依「鮮豔度×佔比」排序，優先鮮色、避開近黑/近白/灰，
不足再用出現頻率補足。失敗/空 URL 一律回 []（graceful，前端退回生成式）。
"""
from __future__ import annotations

import asyncio
import colorsys
import io
from typing import Awaitable, Callable, List, Optional

try:
    import aiohttp
except Exception:  # pragma: no cover
    aiohttp = None


async def _download(url: str, timeout_s: float = 6.0) -> Optional[bytes]:
    if aiohttp is None:
        return None
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=timeout_s)) as resp:
                if resp.status != 200:
                    return None
                return await resp.read()
    except Exception:
        return None


def _palette_from_bytes(data: bytes, n: int) -> List[str]:
    from PIL import Image

    img = Image.open(io.BytesIO(data)).convert("RGB").resize((72, 72))
    q = img.quantize(colors=16)  # 預設 median-cut
    pal = q.getpalette() or []
    counts = q.getcolors() or []  # [(count, index), ...]

    scored = []  # (count, vividness, r, g, b)
    for count, idx in counts:
        r, g, b = pal[idx * 3: idx * 3 + 3]
        _, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        vivid = 0.0 if (v < 0.12 or v > 0.97) else s * v  # 飽和×亮度：暗色排後、亮鮮色優先
        scored.append((count, vivid, r, g, b))

    vivid_sorted = sorted(
        (c for c in scored if c[1] > 0.12), key=lambda c: c[1] * c[0], reverse=True
    )
    freq_sorted = sorted(scored, key=lambda c: c[0], reverse=True)

    out: List[str] = []
    seen = set()
    for c in list(vivid_sorted) + freq_sorted:  # 鮮色優先，不足用頻率補
        hexv = "#%02X%02X%02X" % (c[2], c[3], c[4])
        if hexv not in seen:
            seen.add(hexv)
            out.append(hexv)
        if len(out) >= n:
            break
    return out


async def extract_palette(
    url: Optional[str],
    *,
    n: int = 4,
    fetch_bytes: Optional[Callable[..., Awaitable[Optional[bytes]]]] = None,
    timeout_s: float = 6.0,
) -> List[str]:
    """回封面主色 hex 陣列（最多 n 個）；空 URL/下載失敗/解析失敗 → []。"""
    if not url:
        return []
    data = await (fetch_bytes or _download)(url, timeout_s)
    if not data:
        return []
    try:
        return await asyncio.to_thread(_palette_from_bytes, data, n)
    except Exception:
        return []

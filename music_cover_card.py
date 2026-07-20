"""歌曲卡合成：封面全幅 + 點播者頭像圓形徽章疊右下角 → PNG bytes。

Discord embed 疊不了圖，故真的用 PIL 合成一張圖上傳。純函式（進出都 bytes）、可單測無網路。
點播者頭像由 caller 決定（Marvin 推薦→bot 頭像；真人→其 Discord 頭像）。

抽色可用時（title+primary+secondary）：底部加一條**主色標題帶、歌名用副色**畫上去
（Discord embed 文字改不了色，唯有畫在圖上）。
"""
from __future__ import annotations

import io
import os

# macOS 內建 CJK 字型（歌名多為中文）；缺則退 PIL 預設（英數可）
_FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
]


def _load_font(size: int):
    from PIL import ImageFont
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _hex(h):
    h = (h or "").lstrip("#")
    if len(h) != 6:
        return None
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def _fit(text: str, font, maxw: float) -> str:
    """截斷到 maxw 內，尾加 …。"""
    try:
        glen = font.getlength
    except Exception:
        glen = lambda s: len(s) * getattr(font, "size", 16) * 0.6  # noqa: E731
    if glen(text) <= maxw:
        return text
    s = text
    while s and glen(s + "…") > maxw:
        s = s[:-1]
    return (s + "…") if s else text[:1]


def compose_cover_with_avatar(
    cover_png: bytes,
    avatar_png: bytes,
    *,
    width: int = 640,
    title: str = "",
    primary: str | None = None,
    secondary: str | None = None,
) -> bytes:
    """封面(全幅, 縮到 width) + 頭像圓形徽章(白框)疊右下角 → PNG bytes。

    給 title+primary+secondary 時，底部加主色標題帶、歌名用副色。
    任一必要輸入壞/缺 → raise（caller 自行 fallback 純封面）。
    """
    from PIL import Image, ImageDraw

    cover = Image.open(io.BytesIO(cover_png)).convert("RGBA")
    w, h = cover.size
    if w <= 0 or h <= 0:
        raise ValueError("bad cover size")
    nh = max(1, int(h * width / w))
    cover = cover.resize((width, nh))

    badge = max(24, int(width * 0.18))          # 頭像徑 ~18% 寬

    # 標題帶：主色底 + 副色字（抽色可用時才畫）
    p, s = _hex(primary), _hex(secondary)
    if title and p and s:
        band_h = max(36, int(width * 0.14))
        band_y = nh - band_h
        band = Image.new("RGBA", (width, band_h), (p[0], p[1], p[2], 255))
        cover.alpha_composite(band, (0, band_y))
        font = _load_font(max(14, int(band_h * 0.52)))
        pad = max(10, int(width * 0.035))
        max_text_w = width - pad - badge - int(width * 0.02)   # 右側留給頭像
        txt = _fit(str(title), font, max_text_w)
        fsize = int(getattr(font, "size", band_h * 0.5))
        ty = band_y + max(0, (band_h - fsize) // 2)
        ImageDraw.Draw(cover).text((pad, ty), txt, fill=(s[0], s[1], s[2], 255), font=font)

    # 頭像圓徽（右下）
    av = Image.open(io.BytesIO(avatar_png)).convert("RGBA").resize((badge, badge))
    mask = Image.new("L", (badge, badge), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, badge - 1, badge - 1), fill=255)

    ring = badge + 8                            # 白色外框圈
    ring_img = Image.new("RGBA", (ring, ring), (0, 0, 0, 0))
    ImageDraw.Draw(ring_img).ellipse((0, 0, ring - 1, ring - 1), fill=(255, 255, 255, 235))

    margin = max(6, int(width * 0.03))
    ax = width - badge - margin
    ay = nh - badge - margin
    cover.alpha_composite(ring_img, (ax - 4, ay - 4))
    cover.paste(av, (ax, ay), mask)

    out = io.BytesIO()
    cover.convert("RGB").save(out, format="PNG")
    return out.getvalue()

"""歌曲卡合成：封面全幅 + 點播者頭像圓形徽章疊右下角 → PNG bytes。

Discord embed 疊不了圖，故真的用 PIL 合成一張圖上傳。純函式（進出都 bytes）、可單測無網路。
點播者頭像由 caller 決定（Marvin 推薦→bot 頭像；真人→其 Discord 頭像）。
"""
from __future__ import annotations

import io


def compose_cover_with_avatar(cover_png: bytes, avatar_png: bytes, *, width: int = 640) -> bytes:
    """封面(全幅, 縮到 width) + 頭像圓形徽章(白框)疊右下角 → PNG bytes。

    任一輸入壞/缺 → raise（caller 自行 fallback 純封面）。
    """
    from PIL import Image, ImageDraw

    cover = Image.open(io.BytesIO(cover_png)).convert("RGBA")
    w, h = cover.size
    if w <= 0 or h <= 0:
        raise ValueError("bad cover size")
    nh = max(1, int(h * width / w))
    cover = cover.resize((width, nh))

    badge = max(24, int(width * 0.18))          # 頭像徑 ~18% 寬
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

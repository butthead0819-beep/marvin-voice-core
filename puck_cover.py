"""車機 AMOLED 圓屏封面合成：凸透鏡 + 玻璃感 + 點播者頭像 → JPEG bytes。

凸透鏡：中央 inner 半徑內等比放大、不變形；外圍用二次曲線壓縮回邊界，
兩段在 inner 處一階導數連續（看起來像真的玻璃鏡片）。
玻璃感：外圈輕微色差（R/B 反向縮放）、邊緣暗角、左上高光、外圈一圈亮線。
點播者頭像貼在玻璃效果之後（不經過透鏡、不吃高光），放右下 45°、白框圓形徽章。
輸出 baseline（非 progressive）JPEG，給 ESP32-S3 解碼。
"""
from __future__ import annotations

import io
import math

import numpy as np


def lens_radius(r, *, center_scale: float = 0.8, inner: float = 0.7):
    """凸透鏡半徑映射：r<=inner 線性放大，r>inner 二次曲線壓回 1，在 inner 處一階導數連續。"""
    a = center_scale
    r0 = inner
    b = (1 - a) / (1 - r0) ** 2
    r_arr = np.asarray(r, dtype=np.float64)
    linear = a * r_arr
    outer = a * r_arr + b * (r_arr - r0) ** 2
    out = np.where(r_arr <= r0, linear, outer)
    if np.isscalar(r) or isinstance(r, (int, float)):
        return float(out)
    return out


def _smoothstep(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def render_puck_cover_image(
    cover_bytes: bytes,
    avatar_bytes: bytes | None = None,
    *,
    size: int = 466,
    center_scale: float = 0.8,
    inner: float = 0.7,
    chroma: float = 0.012,
    vignette: float = 0.35,
    highlight: float = 0.22,
    rim: float = 0.35,
):
    from PIL import Image, ImageDraw

    cover = Image.open(io.BytesIO(cover_bytes)).convert("RGB")
    w, h = cover.size
    if w != h:
        n = min(w, h)
        left = (w - n) // 2
        top = (h - n) // 2
        cover = cover.crop((left, top, left + n, top + n))
    src = np.asarray(cover, dtype=np.float32)
    n = src.shape[0]

    R = size / 2.0
    xs = np.arange(size, dtype=np.float64)
    ys = np.arange(size, dtype=np.float64)
    gx, gy = np.meshgrid(xs, ys)
    nx = (gx + 0.5 - R) / R
    ny = (gy + 0.5 - R) / R
    r = np.hypot(nx, ny)
    r_safe = np.where(r == 0, 1.0, r)

    f = lens_radius(r_safe, center_scale=center_scale, inner=inner)
    scale = np.where(r == 0, center_scale, f / r_safe)

    t = np.clip((r - inner) / (1 - inner), 0.0, 1.0)
    wgt = t * t * (3 - 2 * t)

    scale_r = scale * (1 + chroma * wgt)
    scale_g = scale
    scale_b = scale * (1 - chroma * wgt)

    def _sample(channel_idx, s):
        sx = np.clip(nx * s, -1.0, 1.0)
        sy = np.clip(ny * s, -1.0, 1.0)
        px = np.clip((sx + 1) / 2 * n - 0.5, 0, n - 1)
        py = np.clip((sy + 1) / 2 * n - 0.5, 0, n - 1)
        x0 = np.floor(px).astype(np.int64)
        y0 = np.floor(py).astype(np.int64)
        x1 = np.clip(x0 + 1, 0, n - 1)
        y1 = np.clip(y0 + 1, 0, n - 1)
        fx = px - x0
        fy = py - y0
        plane = src[:, :, channel_idx]
        v00 = plane[y0, x0]
        v01 = plane[y0, x1]
        v10 = plane[y1, x0]
        v11 = plane[y1, x1]
        top_v = v00 * (1 - fx) + v01 * fx
        bot_v = v10 * (1 - fx) + v11 * fx
        return top_v * (1 - fy) + bot_v * fy

    out_r = _sample(0, scale_r)
    out_g = _sample(1, scale_g)
    out_b = _sample(2, scale_b)
    out = np.stack([out_r, out_g, out_b], axis=-1)

    out *= (1 - vignette * _smoothstep(0.88, 1.0, r))[:, :, None]

    d2 = (nx + 0.33) ** 2 + (ny + 0.42) ** 2
    hl = highlight * np.exp(-d2 / (2 * 0.28 ** 2))
    out = out + (255 - out) * hl[:, :, None]

    rim_mask = (r >= 1 - 2.5 / R) & (r <= 1 - 0.5 / R)
    out[rim_mask] = out[rim_mask] + (255 - out[rim_mask]) * rim

    out[r > 1] = 0

    out = np.clip(out, 0, 255).astype(np.uint8)
    result = Image.fromarray(out, mode="RGB")

    if avatar_bytes is not None:
        try:
            av = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
            d = round(0.2 * size)
            av = av.resize((d, d))
            mask = Image.new("L", (d, d), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, d - 1, d - 1), fill=255)

            ring_d = d + 8
            ring_img = Image.new("RGBA", (ring_d, ring_d), (0, 0, 0, 0))
            ImageDraw.Draw(ring_img).ellipse((0, 0, ring_d - 1, ring_d - 1), fill=(255, 255, 255, 235))

            cx = R + 0.62 * R * math.cos(math.pi / 4)
            cy = R + 0.62 * R * math.sin(math.pi / 4)

            av_left = round(cx - d / 2)
            av_top = round(cy - d / 2)

            result_rgba = result.convert("RGBA")
            result_rgba.alpha_composite(ring_img, (av_left - 4, av_top - 4))
            result_rgba.paste(av, (av_left, av_top), mask)
            result = result_rgba.convert("RGB")
        except Exception:
            pass

    return result


def render_puck_cover(cover_bytes: bytes, avatar_bytes: bytes | None = None, **kw) -> bytes:
    img = render_puck_cover_image(cover_bytes, avatar_bytes, **kw)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85, progressive=False)
    return out.getvalue()

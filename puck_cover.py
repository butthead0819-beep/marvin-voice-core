"""車機圓屏封面合成：徽章別針（lapel pin）造型 → JPEG bytes。

造型：封面圖印在平頂（flat 半徑內）上，外圈從 flat 到邊界沿 1/4 圓弧捲邊往後包
（像徽章別針邊緣），印刷紙貼合捲邊一路包過去（不是裁切/模糊）。
捲邊打光：依捲邊法向量算光源（左上）入射角，平頂法向量朝正前方、打光係數恰為 1，
只有捲邊會因角度變暗/變亮；疊一層高光（只在捲邊，模擬邊緣反光）+ 一層固定位置的
亮面膜高光（模擬印刷表面膜層）。
輸出 baseline（非 progressive）JPEG，給 ESP32-S3 解碼。
"""
from __future__ import annotations

import io
import math

import numpy as np


def pin_radius(r, *, flat: float = 0.8):
    """徽章別針半徑映射：r<=flat 原樣（平頂），r>flat 沿 1/4 圓弧捲邊壓回 1。"""
    rho = 1.0 - flat
    smax = flat + rho * (math.pi / 2)
    r_arr = np.asarray(r, dtype=np.float64)
    theta = np.arcsin(np.clip((r_arr - flat) / rho, 0.0, 1.0))
    s = np.where(r_arr <= flat, r_arr, flat + rho * theta)
    out = s / smax
    if np.isscalar(r) or isinstance(r, (int, float)):
        return float(out)
    return out


def render_puck_cover_image(
    cover_bytes: bytes,
    *,
    size: int = 466,
    flat: float = 0.8,
    gloss: float = 0.18,
    spec: float = 0.6,
    ambient: float = 0.35,
):
    from PIL import Image

    cover = Image.open(io.BytesIO(cover_bytes)).convert("RGB")
    w, h = cover.size
    if w != h:
        n = min(w, h)
        left = (w - n) // 2
        top = (h - n) // 2
        cover = cover.crop((left, top, left + n, top + n))
    src = np.asarray(cover, dtype=np.float32)
    n = src.shape[0]

    rho = 1.0 - flat
    smax = flat + rho * (math.pi / 2)

    R = size / 2.0
    xs = np.arange(size, dtype=np.float64)
    ys = np.arange(size, dtype=np.float64)
    gx, gy = np.meshgrid(xs, ys)
    nx = (gx + 0.5 - R) / R
    ny = (gy + 0.5 - R) / R
    r = np.hypot(nx, ny)
    r_safe = np.where(r == 0, 1.0, r)

    s = pin_radius(r_safe, flat=flat)
    scale = np.where(r == 0, 1.0 / smax, s / r_safe)

    sx = np.clip(nx * scale, -1.0, 1.0)
    sy = np.clip(ny * scale, -1.0, 1.0)
    px = np.clip((sx + 1) / 2 * n - 0.5, 0, n - 1)
    py = np.clip((sy + 1) / 2 * n - 0.5, 0, n - 1)
    x0 = np.floor(px).astype(np.int64)
    y0 = np.floor(py).astype(np.int64)
    x1 = np.clip(x0 + 1, 0, n - 1)
    y1 = np.clip(y0 + 1, 0, n - 1)
    fx = px - x0
    fy = py - y0

    def _sample(channel_idx):
        plane = src[:, :, channel_idx]
        v00 = plane[y0, x0]
        v01 = plane[y0, x1]
        v10 = plane[y1, x0]
        v11 = plane[y1, x1]
        top_v = v00 * (1 - fx) + v01 * fx
        bot_v = v10 * (1 - fx) + v11 * fx
        return top_v * (1 - fy) + bot_v * fy

    out_r = _sample(0)
    out_g = _sample(1)
    out_b = _sample(2)
    out = np.stack([out_r, out_g, out_b], axis=-1)

    # 捲邊角度 + 法向量（平頂 r<=flat 時法向量朝正前方 (0,0,1)）。
    theta = np.where(r <= flat, 0.0, np.arcsin(np.clip((r - flat) / rho, 0.0, 1.0)))
    ux = np.where(r == 0, 0.0, nx / r_safe)
    uy = np.where(r == 0, 0.0, ny / r_safe)
    sin_t = np.sin(theta)
    Nx = ux * sin_t
    Ny = uy * sin_t
    Nz = np.cos(theta)

    Lx, Ly, Lz = -0.35, -0.45, 0.82
    l_norm = math.sqrt(Lx * Lx + Ly * Ly + Lz * Lz)
    Lx, Ly, Lz = Lx / l_norm, Ly / l_norm, Lz / l_norm

    dot_nl = Nx * Lx + Ny * Ly + Nz * Lz
    light = (ambient + (1 - ambient) * np.maximum(0.0, dot_nl)) / (ambient + (1 - ambient) * Lz)
    out = out * light[:, :, None]

    Hx, Hy, Hz = Lx, Ly, Lz + 1.0
    h_norm = math.sqrt(Hx * Hx + Hy * Hy + Hz * Hz)
    Hx, Hy, Hz = Hx / h_norm, Hy / h_norm, Hz / h_norm
    dot_nh = Nx * Hx + Ny * Hy + Nz * Hz
    sp = spec * np.maximum(0.0, dot_nh) ** 40
    sp = np.where(r > flat, sp, 0.0)

    d2 = (nx + 0.33) ** 2 + (ny + 0.42) ** 2
    gl = gloss * np.exp(-d2 / (2 * 0.3 ** 2))

    a = np.clip(sp + gl, 0.0, 1.0)
    out = out + (255 - out) * a[:, :, None]

    out[r > 1] = 0

    out = np.clip(out, 0, 255).astype(np.uint8)
    return Image.fromarray(out)


def render_puck_cover(cover_bytes: bytes, **kw) -> bytes:
    img = render_puck_cover_image(cover_bytes, **kw)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85, progressive=False)
    return out.getvalue()

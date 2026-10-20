"""車機圓屏封面合成 puck_cover — 純函式、無網路。"""
import io

import pytest

pytest.importorskip("PIL")
pytest.importorskip("numpy")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from puck_cover import lens_radius, render_puck_cover, render_puck_cover_image  # noqa: E402


def _png(w, h, color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _gradient(n=600) -> bytes:
    xs = np.arange(n, dtype=np.float32)
    row_r = xs * 255 / (n - 1)
    r = np.tile(row_r[None, :], (n, 1))
    ys = np.arange(n, dtype=np.float32)
    col_g = ys * 255 / (n - 1)
    g = np.tile(col_g[:, None], (1, n))
    b = np.zeros((n, n), dtype=np.float32)
    arr = np.stack([r, g, b], axis=-1).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr, mode="RGB").save(buf, format="PNG")
    return buf.getvalue()


def test_lens_radius_known_values():
    assert lens_radius(0.0) == 0.0
    assert abs(lens_radius(0.35) - 0.28) < 1e-9
    assert abs(lens_radius(0.7) - 0.56) < 1e-9
    assert abs(lens_radius(1.0) - 1.0) < 1e-9
    assert abs(lens_radius(0.85) - 0.73) < 1e-9


def test_lens_radius_monotonic_and_derivative_continuity():
    rs = np.linspace(0, 1, 1001)
    fs = lens_radius(rs)
    diffs = np.diff(fs)
    assert np.all(diffs > 0)

    h = 1e-6
    left = (lens_radius(0.7) - lens_radius(0.7 - h)) / h
    right = (lens_radius(0.7 + h) - lens_radius(0.7)) / h
    assert abs(left - right) < 1e-3

    left_deriv_at_1 = (lens_radius(1.0) - lens_radius(1.0 - h)) / h
    assert abs(left_deriv_at_1 - 2.1333) < 1e-3


def test_lens_radius_numpy_array_shape():
    rs = np.linspace(0, 1, 50)
    fs = lens_radius(rs)
    assert isinstance(fs, np.ndarray)
    assert fs.shape == rs.shape


def test_size_and_black_corners():
    cover = _png(600, 600, (120, 60, 30))
    img = render_puck_cover_image(cover)
    assert img.size == (466, 466)
    assert img.mode == "RGB"
    for x, y in [(0, 0), (465, 0), (0, 465), (465, 465)]:
        px = img.getpixel((x, y))
        assert px == (0, 0, 0)


def test_center_magnification_undistorted():
    cover = _gradient(600)
    img = render_puck_cover_image(cover, chroma=0, vignette=0, highlight=0, rim=0)
    r1 = img.getpixel((293, 232))[0]
    r2 = img.getpixel((353, 232))[0]
    assert abs(r1 - 154.0) <= 2.0
    assert abs(r2 - 180.3) <= 2.0


def test_outer_compression():
    cover = _gradient(600)
    img = render_puck_cover_image(cover, chroma=0, vignette=0, highlight=0, rim=0)
    r3 = img.getpixel((458, 232))[0]
    # 若外圈沿用內圈線性會是 ≈226，所以這條能區分線性 vs 二次壓縮
    assert abs(r3 - 246.7) <= 2.0


def test_chroma_only_outside_inner():
    cover = _gradient(600)
    img_no_chroma = render_puck_cover_image(cover, chroma=0, vignette=0, highlight=0, rim=0)
    img_chroma = render_puck_cover_image(cover, chroma=0.05, vignette=0, highlight=0, rim=0)

    r_center_no = img_no_chroma.getpixel((293, 232))[0]
    r_center_c = img_chroma.getpixel((293, 232))[0]
    assert abs(r_center_c - r_center_no) <= 0.5

    r_outer_no = img_no_chroma.getpixel((458, 232))[0]
    r_outer_c = img_chroma.getpixel((458, 232))[0]
    assert r_outer_c - r_outer_no >= 1.0


def test_vignette_darkens_edge():
    cover = _png(600, 600, (128, 128, 128))
    img_v0 = render_puck_cover_image(cover, vignette=0, highlight=0, rim=0, chroma=0)
    img_v = render_puck_cover_image(cover, vignette=0.35, highlight=0, rim=0, chroma=0)
    r_v0 = img_v0.getpixel((458, 232))[0]
    r_v = img_v.getpixel((458, 232))[0]
    assert r_v0 - r_v >= 10


def test_highlight_brightens_top_left():
    cover = _png(600, 600, (10, 10, 10))
    cx = round(233 - 0.33 * 233)
    cy = round(233 - 0.42 * 233)
    img_h = render_puck_cover_image(cover, vignette=0, rim=0, highlight=0.22)
    img_h0 = render_puck_cover_image(cover, vignette=0, rim=0, highlight=0)
    assert img_h.getpixel((cx, cy))[0] >= 50
    assert img_h0.getpixel((cx, cy))[0] <= 12


def test_avatar_badge_overlay():
    cover = _png(600, 600, (0, 0, 255))
    avatar = _png(128, 128, (255, 0, 0))
    img_with = render_puck_cover_image(cover, avatar)
    px = img_with.getpixel((335, 335))
    assert px[0] > 200
    assert px[2] < 60

    img_without = render_puck_cover_image(cover, None)
    px2 = img_without.getpixel((335, 335))
    assert px2[2] > 150
    assert px2[0] < 60


def test_bad_avatar_falls_back_to_none():
    cover = _png(600, 600, (0, 0, 255))
    img_bad = render_puck_cover_image(cover, b"xx")
    img_none = render_puck_cover_image(cover, None)
    assert np.array_equal(np.asarray(img_bad), np.asarray(img_none))


def test_bad_cover_raises():
    with pytest.raises(Exception):
        render_puck_cover_image(b"not-an-image")


def test_non_square_cover_center_cropped():
    w, h = 1280, 720
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[:, :280] = (0, 255, 0)
    arr[:, 280:280 + 720] = (0, 0, 255)
    arr[:, 280 + 720:] = (0, 255, 0)
    buf = io.BytesIO()
    Image.fromarray(arr, mode="RGB").save(buf, format="PNG")
    cover = buf.getvalue()

    img = render_puck_cover_image(cover, chroma=0, vignette=0, highlight=0, rim=0)
    for x, y in [(26, 232), (233, 232), (440, 232)]:
        px = img.getpixel((x, y))
        assert px[2] > 200
        assert px[1] < 60
    # (26,232) 在左側：沒裁切的話會取到左邊 280px 的綠


def test_render_puck_cover_returns_baseline_jpeg():
    cover = _png(600, 600, (50, 80, 120))
    out = render_puck_cover(cover)
    assert out[:2] == b"\xff\xd8"
    img = Image.open(io.BytesIO(out))
    assert img.format == "JPEG"
    assert img.size == (466, 466)
    assert "progressive" not in img.info and "progression" not in img.info


def test_rim_brightens_outer_ring():
    cover = _png(600, 600, (10, 10, 10))
    img_r = render_puck_cover_image(cover, vignette=0, highlight=0, chroma=0, rim=0.35)
    img_r0 = render_puck_cover_image(cover, vignette=0, highlight=0, chroma=0, rim=0)
    # (464,232)：r≈0.9936，落在亮線帶 [1-2.5/R, 1-0.5/R]
    assert img_r.getpixel((464, 232))[0] >= 80
    assert img_r0.getpixel((464, 232))[0] <= 12
    # (440,232) 在亮線帶內側，不受影響
    assert img_r.getpixel((440, 232))[0] <= 12

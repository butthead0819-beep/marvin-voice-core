"""車機圓屏封面合成 puck_cover — 純函式、無網路。"""
import io

import pytest

pytest.importorskip("PIL")
pytest.importorskip("numpy")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from puck_cover import pin_radius, render_puck_cover, render_puck_cover_image  # noqa: E402


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


def test_pin_radius_known_values():
    assert pin_radius(0.0) == 0.0
    assert abs(pin_radius(0.4) - 0.3590151) < 1e-6
    assert abs(pin_radius(0.8) - 0.7180302) < 1e-6
    assert abs(pin_radius(0.9) - 0.8120201) < 1e-6
    assert abs(pin_radius(1.0) - 1.0) < 1e-6


def test_pin_radius_monotonic_and_derivative_continuity():
    rs = np.linspace(0, 1, 1001)
    fs = pin_radius(rs)
    diffs = np.diff(fs)
    assert np.all(diffs > 0)

    h = 1e-6
    left = (pin_radius(0.8) - pin_radius(0.8 - h)) / h
    right = (pin_radius(0.8 + h) - pin_radius(0.8)) / h
    assert abs(left - 0.8975377) < 1e-4
    assert abs(right - 0.8975377) < 1e-4


def test_pin_radius_numpy_array_shape():
    rs = np.linspace(0, 1, 50)
    fs = pin_radius(rs)
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


def test_flat_top_undistorted_magnification():
    cover = _gradient(600)
    img = render_puck_cover_image(cover, gloss=0, spec=0, ambient=1.0)
    r1 = img.getpixel((293, 232))[0]
    r2 = img.getpixel((353, 232))[0]
    assert abs(r1 - 157.3) <= 2.0
    assert abs(r2 - 186.8) <= 2.0


def test_rim_compression():
    cover = _gradient(600)
    img = render_puck_cover_image(cover, gloss=0, spec=0, ambient=1.0)
    r3 = img.getpixel((463, 232))[0]
    # 若沿用平頂線性會是 ≈240.9，這條能區分線性 vs 1/4 圓弧捲邊壓縮
    assert abs(r3 - 247.7) <= 1.5


def test_flat_top_unaffected_by_lighting():
    cover = _gradient(600)
    img_default = render_puck_cover_image(cover, gloss=0)
    img_full_ambient = render_puck_cover_image(cover, gloss=0, spec=0, ambient=1.0)
    r_default = img_default.getpixel((293, 232))[0]
    r_full = img_full_ambient.getpixel((293, 232))[0]
    assert abs(r_default - r_full) <= 0.5


def test_rim_lighting_top_left_brighter():
    cover = _png(600, 600, (128, 128, 128))
    img = render_puck_cover_image(cover, gloss=0, spec=0)
    r_tl = img.getpixel((76, 76))[0]
    r_br = img.getpixel((389, 389))[0]
    assert r_tl - r_br >= 15


def test_rim_specular_highlight():
    cover = _png(600, 600, (128, 128, 128))
    img_spec = render_puck_cover_image(cover, gloss=0, spec=0.6)
    img_nospec = render_puck_cover_image(cover, gloss=0, spec=0)
    r_peak_spec = img_spec.getpixel((110, 74))[0]
    r_peak_nospec = img_nospec.getpixel((110, 74))[0]
    assert r_peak_spec - r_peak_nospec >= 30

    r_br_spec = img_spec.getpixel((389, 389))[0]
    r_br_nospec = img_nospec.getpixel((389, 389))[0]
    assert abs(r_br_spec - r_br_nospec) <= 1


def test_gloss_film_highlight():
    cover = _png(600, 600, (10, 10, 10))
    cx = round(233 - 0.33 * 233)
    cy = round(233 - 0.42 * 233)
    img_gloss = render_puck_cover_image(cover, spec=0, gloss=0.18)
    img_nogloss = render_puck_cover_image(cover, spec=0, gloss=0)
    assert img_gloss.getpixel((cx, cy))[0] >= 45
    assert img_nogloss.getpixel((cx, cy))[0] <= 12


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

    img = render_puck_cover_image(cover, gloss=0, spec=0, ambient=1.0)
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

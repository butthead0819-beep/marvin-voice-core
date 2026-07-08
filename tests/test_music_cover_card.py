"""歌曲卡合成 compose_cover_with_avatar — 純函式、無網路。"""
import io

import pytest

pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from music_cover_card import compose_cover_with_avatar  # noqa: E402


def _png(w, h, color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def test_composes_valid_png_at_target_width():
    cover = _png(1280, 720, (20, 40, 80))
    avatar = _png(128, 128, (200, 50, 50))
    out = compose_cover_with_avatar(cover, avatar, width=640)
    img = Image.open(io.BytesIO(out))
    assert img.format == "PNG"
    assert img.size[0] == 640                 # 縮到目標寬
    assert img.size[1] == 360                 # 保持 16:9 比例


def test_square_cover_keeps_aspect():
    out = compose_cover_with_avatar(_png(500, 500, (0, 0, 0)), _png(64, 64, (255, 255, 255)), width=640)
    img = Image.open(io.BytesIO(out))
    assert img.size == (640, 640)


def test_bad_cover_raises_for_caller_fallback():
    with pytest.raises(Exception):
        compose_cover_with_avatar(b"not-an-image", _png(64, 64, (1, 1, 1)))

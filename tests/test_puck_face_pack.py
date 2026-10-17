"""tests/test_puck_face_pack.py — tools/puck_face/face_pack.py 的純函式測試。

用手工構造的小型 baked dict（不依賴 bake.py / Chrome）驗 pack/unpack 往返、header
固定位置欄位、段落 4-byte 對齊、像素位元組順序（big-endian）、錯誤路徑。
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "puck_face"))

import face_pack  # noqa: E402


def _px(value: int, n: int) -> bytes:
    """n 個 RGB565 像素，值都是 value，big-endian。"""
    return struct.pack(">" + "H" * n, *([value] * n))


def _b64(b: bytes) -> str:
    import base64
    return base64.b64encode(b).decode("ascii")


def _small_baked():
    """width=height=8 的小型 baked dict：三個 clip（still/once/ladder）。"""
    w = h = 8
    base = _px(0x0000, w * h)
    return {
        "fps": 24, "width": w, "height": h,
        "base": _b64(base),
        "clips": [
            {"id": "still", "kind": "once", "loop_from": 0, "psram": False,
             "rect": None, "frames": [], "seq": []},
            {"id": "newsong", "kind": "once", "loop_from": 0, "psram": False,
             "rect": {"x": 2, "y": 2, "w": 2, "h": 2},
             "frames": [_b64(_px(0x1234, 4)), _b64(_px(0x5678, 4))],
             "seq": [0, 1, 0]},
            {"id": "nod", "kind": "ladder", "loop_from": 0, "psram": True,
             "rect": {"x": 0, "y": 0, "w": 4, "h": 2},
             "frames": [_b64(_px(0x0001, 8)), _b64(_px(0x0002, 8)), _b64(_px(0x0003, 8))],
             "seq": [0, 1, 2, 1, 0]},
        ],
    }


def test_pack_unpack_round_trip_fields():
    baked = _small_baked()
    data = face_pack.pack(baked)
    out = face_pack.unpack(data)

    assert out["fps"] == baked["fps"]
    assert out["width"] == baked["width"]
    assert out["height"] == baked["height"]

    import base64
    assert out["base"] == base64.b64decode(baked["base"])

    assert len(out["clips"]) == 3
    still, newsong, nod = out["clips"]

    assert still["id"] == "still"
    assert still["kind"] == "once"
    assert still["psram"] is False
    assert still["rect"] is None
    assert still["seq"] == []

    assert newsong["id"] == "newsong"
    assert newsong["kind"] == "once"
    assert newsong["rect"] == {"x": 2, "y": 2, "w": 2, "h": 2}
    # newsong 的全域 frame id 緊接在 still 之後（still 沒有 frame，所以從 0 開始）
    assert newsong["seq"] == [0, 1, 0]

    assert nod["id"] == "nod"
    assert nod["kind"] == "ladder"
    assert nod["psram"] is True
    # nod 的全域 id 接在 newsong 的兩個 uniq frame 之後 -> 從 2 開始
    assert nod["seq"] == [2, 3, 4, 3, 2]

    # frame pixels 內容逐位元組相等
    assert out["frames"][0]["pixels"] == base64.b64decode(baked["clips"][1]["frames"][0])
    assert out["frames"][1]["pixels"] == base64.b64decode(baked["clips"][1]["frames"][1])
    assert out["frames"][2]["pixels"] == base64.b64decode(baked["clips"][2]["frames"][0])
    assert out["frames"][3]["pixels"] == base64.b64decode(baked["clips"][2]["frames"][1])
    assert out["frames"][4]["pixels"] == base64.b64decode(baked["clips"][2]["frames"][2])


def test_header_fixed_fields_direct_read():
    baked = _small_baked()
    data = face_pack.pack(baked)

    magic, version, fps, width, height, n_clips, n_frames = struct.unpack_from(
        "<4sHHHHHH", data, 0)
    assert magic == b"MFC1"
    assert version == 1
    assert fps == 24
    assert width == 8
    assert height == 8
    assert n_clips == 3
    assert n_frames == 5  # still:0 + newsong:2 uniq + nod:3 uniq

    base_offset, clips_offset, frames_offset, total_size = struct.unpack_from(
        "<IIII", data, 16)
    assert total_size == len(data)
    assert clips_offset == face_pack._align4(face_pack.HEADER_SIZE)
    assert frames_offset == face_pack._align4(clips_offset + 3 * face_pack.CLIP_ENTRY_SIZE)
    assert base_offset >= frames_offset + n_frames * face_pack.FRAME_ENTRY_SIZE


def test_section_offsets_are_4_byte_aligned():
    baked = _small_baked()
    data = face_pack.pack(baked)
    base_offset, clips_offset, frames_offset, _total = struct.unpack_from("<IIII", data, 16)
    assert clips_offset % 4 == 0
    assert frames_offset % 4 == 0
    assert base_offset % 4 == 0
    # 每一格像素的起點也要對齊（S3 會整塊讀進 PSRAM）：直接讀 frame table 的 pixel_offset
    n_frames = struct.unpack_from("<H", data, 14)[0]
    assert n_frames > 0
    for i in range(n_frames):
        pixel_offset = struct.unpack_from("<I", data, frames_offset + i * 12)[0]
        assert pixel_offset % 4 == 0


def test_pixel_byte_order_is_big_endian():
    baked = _small_baked()
    # newsong 的第 1 個 frame 固定是 0x1234 像素
    data = face_pack.pack(baked)
    out = face_pack.unpack(data)
    pixels = out["frames"][0]["pixels"]
    assert pixels[0:2] == b"\x12\x34"


def test_frame_size_mismatch_raises():
    baked = _small_baked()
    # 把 newsong 第一個 frame 改成錯誤長度（rect 是 2x2=4 像素，這裡只給 2 個）
    baked["clips"][1]["frames"][0] = _b64(_px(0x1234, 2))
    try:
        face_pack.pack(baked)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_id_too_long_raises():
    baked = _small_baked()
    baked["clips"][0]["id"] = "a" * 16  # 16 bytes > 15
    try:
        face_pack.pack(baked)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_unpack_bad_magic_raises():
    baked = _small_baked()
    data = bytearray(face_pack.pack(baked))
    data[0:4] = b"XXXX"
    try:
        face_pack.unpack(bytes(data))
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_unpack_bad_total_size_raises():
    baked = _small_baked()
    data = face_pack.pack(baked) + b"\x00" * 4  # 尾端多塞 4 bytes，total_size 不再等於 len(data)
    try:
        face_pack.unpack(data)
        assert False, "expected ValueError"
    except ValueError:
        pass

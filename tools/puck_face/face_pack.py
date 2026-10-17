"""face_pack.py — 把 bake.html/face_render.js 烤出的 JSON（base64 RGB565）封裝成二進位
格式 MFC1，給 S3 韌體讀。純函式，無 IO。

版面（除像素外一律 little-endian；像素是 RGB565 big-endian，面板原生順序）：
  header(32B) -> clip table(n_clips*40B) -> frame table(n_frames*12B)
  -> 各 clip 的 seq 陣列 -> base 像素 -> 各 frame 像素
每一段起點對齊 4 bytes。
"""
from __future__ import annotations

import base64
import struct

MAGIC = b"MFC1"
VERSION = 1
HEADER_SIZE = 32
CLIP_ENTRY_SIZE = 40
FRAME_ENTRY_SIZE = 12

_KIND_TO_CODE = {"once": 0, "loop": 1, "ladder": 2}
_CODE_TO_KIND = {v: k for k, v in _KIND_TO_CODE.items()}


def _align4(n: int) -> int:
    return (n + 3) & ~3


def pack(baked: dict) -> bytes:
    fps = baked["fps"]
    width = baked["width"]
    height = baked["height"]
    clips_in = baked["clips"]
    n_clips = len(clips_in)

    for c in clips_in:
        if len(c["id"].encode("ascii")) > 15:
            raise ValueError(f"clip id too long: {c['id']!r}")

    base_pixels = base64.b64decode(baked["base"])

    # 依 clip 順序把各 clip 的 uniq 串起來，位置即「全域 frame id」。
    global_frames = []  # (x, y, w, h, pixels)
    clip_start_id = []
    for c in clips_in:
        rect = c["rect"]
        clip_start_id.append(len(global_frames))
        for f_b64 in c["frames"]:
            pix = base64.b64decode(f_b64)
            expected = rect["w"] * rect["h"] * 2
            if len(pix) != expected:
                raise ValueError(
                    f"frame size mismatch for clip {c['id']!r}: "
                    f"got {len(pix)} bytes, expected {expected}"
                )
            global_frames.append((rect["x"], rect["y"], rect["w"], rect["h"], pix))

    n_frames = len(global_frames)

    clips_offset = _align4(HEADER_SIZE)
    frames_offset = _align4(clips_offset + n_clips * CLIP_ENTRY_SIZE)
    seq_arrays_offset = _align4(frames_offset + n_frames * FRAME_ENTRY_SIZE)

    seq_offsets = []
    cur = seq_arrays_offset
    for c in clips_in:
        seq_offsets.append(cur)
        cur += len(c["seq"]) * 2
    seq_arrays_size = cur - seq_arrays_offset

    base_offset = _align4(seq_arrays_offset + seq_arrays_size)
    base_size = len(base_pixels)

    frames_pixels_offset = _align4(base_offset + base_size)
    frame_pixel_offsets = []
    cur = frames_pixels_offset
    for (_x, _y, _w, _h, pix) in global_frames:
        frame_pixel_offsets.append(cur)
        cur += len(pix)
    total_size = cur

    buf = bytearray(total_size)
    struct.pack_into("<4sHHHHHH", buf, 0, MAGIC, VERSION, fps, width, height, n_clips, n_frames)
    struct.pack_into("<IIII", buf, 16, base_offset, clips_offset, frames_offset, total_size)

    for i, c in enumerate(clips_in):
        off = clips_offset + i * CLIP_ENTRY_SIZE
        id_field = c["id"].encode("ascii").ljust(16, b"\x00")
        kind_code = _KIND_TO_CODE[c["kind"]]
        flags = 1 if c["psram"] else 0
        rect = c["rect"]
        rx, ry, rw, rh = (rect["x"], rect["y"], rect["w"], rect["h"]) if rect else (0, 0, 0, 0)
        struct.pack_into(
            "<16sBBH4HHHII", buf, off,
            id_field, kind_code, flags, c["loop_from"],
            rx, ry, rw, rh,
            len(c["seq"]), 0, seq_offsets[i], 0,
        )

    for i, c in enumerate(clips_in):
        seq_off = seq_offsets[i]
        for j, local_idx in enumerate(c["seq"]):
            global_id = clip_start_id[i] + local_idx
            struct.pack_into("<H", buf, seq_off + j * 2, global_id)

    buf[base_offset:base_offset + base_size] = base_pixels

    for i, (x, y, w, h, pix) in enumerate(global_frames):
        off = frames_offset + i * FRAME_ENTRY_SIZE
        struct.pack_into("<IHHHH", buf, off, frame_pixel_offsets[i], x, y, w, h)
        poff = frame_pixel_offsets[i]
        buf[poff:poff + len(pix)] = pix

    return bytes(buf)


def unpack(data: bytes) -> dict:
    if len(data) < HEADER_SIZE:
        raise ValueError("data too short for MFC1 header")
    magic, version, fps, width, height, n_clips, n_frames = struct.unpack_from(
        "<4sHHHHHH", data, 0)
    if magic != MAGIC:
        raise ValueError(f"bad magic: {magic!r}")
    if version != VERSION:
        raise ValueError(f"unsupported version: {version}")
    base_offset, clips_offset, frames_offset, total_size = struct.unpack_from(
        "<IIII", data, 16)
    if total_size != len(data):
        raise ValueError(f"total_size mismatch: header says {total_size}, got {len(data)}")

    clips = []
    for i in range(n_clips):
        off = clips_offset + i * CLIP_ENTRY_SIZE
        (id_field, kind_code, flags, loop_from,
         rx, ry, rw, rh, seq_len, _reserved, seq_offset, _reserved2) = struct.unpack_from(
            "<16sBBH4HHHII", data, off)
        cid = id_field.rstrip(b"\x00").decode("ascii")
        rect = None if (rx, ry, rw, rh) == (0, 0, 0, 0) else {"x": rx, "y": ry, "w": rw, "h": rh}
        seq = list(struct.unpack_from(f"<{seq_len}H", data, seq_offset)) if seq_len else []
        clips.append({
            "id": cid,
            "kind": _CODE_TO_KIND[kind_code],
            "psram": bool(flags & 1),
            "loop_from": loop_from,
            "rect": rect,
            "seq": seq,
        })

    frames = []
    for i in range(n_frames):
        off = frames_offset + i * FRAME_ENTRY_SIZE
        pixel_offset, x, y, w, h = struct.unpack_from("<IHHHH", data, off)
        pixels = data[pixel_offset:pixel_offset + w * h * 2]
        frames.append({"x": x, "y": y, "w": w, "h": h, "pixels": pixels})

    base = data[base_offset:base_offset + width * height * 2]

    return {
        "fps": fps, "width": width, "height": height,
        "base": base, "clips": clips, "frames": frames,
    }


def summary(data: bytes) -> str:
    d = unpack(data)
    lines = []
    total = len(d["base"])
    psram_total = 0
    for c in d["clips"]:
        uniq_ids = sorted(set(c["seq"]))
        cbytes = sum(len(d["frames"][gid]["pixels"]) for gid in uniq_ids)
        rect = c["rect"]
        rect_str = f"{rect['w']}x{rect['h']}@({rect['x']},{rect['y']})" if rect else "-"
        lines.append(
            f"{c['id']:<10} kind={c['kind']:<6} seq={len(c['seq']):<4} "
            f"uniq={len(uniq_ids):<4} rect={rect_str:<18} bytes={cbytes}"
        )
        total += cbytes
        if c["psram"]:
            psram_total += cbytes
    lines.append(f"total={total} bytes  psram={psram_total} bytes")
    return "\n".join(lines)

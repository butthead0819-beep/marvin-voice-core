"""推廣素材批次出片：candidates.json → 逐 clip 黑底直式 mp4（頭像、馬文重合成、歌名卡、響度正規化）。

candidates.json 由另一支工具（D1）產生／人工核可；本工具只讀。所有時間相對錄音開頭。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from PIL import Image, ImageDraw, ImageFont

from audio_mixing import TTS_LOUDNESS_AF
from scripts.make_subtitle_video import (
    _default_font,
    _ffprobe_duration,
    build_ffmpeg_cmd,
    wrap_text,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_OUT_DIR = Path.home() / "Documents" / "Marvin-Validation" / "素材"

_PALETTE_COLORS = [
    "#FFF176", "#F48FB1", "#A5D6A7", "#CE93D8",
    "#FFAB91", "#80DEEA", "#E6EE9C", "#BCAAA4",
]
_PALETTE_SHAPES = ["dot", "triangle", "square", "diamond", "star", "hexagon"]

MARVIN_COLOR = "#4FC3F7"
MARMO_COLOR = "#FFB74D"

_CACHE_DIR = Path.home() / ".cache" / "marvin_clip_tts"

_END_PUNCT = "。！？!?…"
_MID_PUNCT = "，、；：,;:"


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# split_for_reading
# ---------------------------------------------------------------------------

def _split_on_chars(text: str, chars: str) -> List[str]:
    parts: List[str] = []
    cur = ""
    for ch in text:
        cur += ch
        if ch in chars:
            parts.append(cur)
            cur = ""
    if cur:
        parts.append(cur)
    return parts


def _hard_split(text: str, max_chars: int) -> List[str]:
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]


def split_for_reading(text: str, max_chars: int) -> List[str]:
    if len(text) <= max_chars:
        return [text] if text else []

    segments: List[str] = []
    for chunk in _split_on_chars(text, _END_PUNCT):
        if len(chunk) <= max_chars:
            segments.append(chunk)
            continue
        for sub in _split_on_chars(chunk, _MID_PUNCT):
            if len(sub) <= max_chars:
                segments.append(sub)
            else:
                segments.extend(_hard_split(sub, max_chars))

    return [s for s in segments if s]


# ---------------------------------------------------------------------------
# Overlay / build_overlays
# ---------------------------------------------------------------------------

@dataclass
class Overlay:
    path: str
    delay_ms: int
    af: str


def _tts_cache_path(voice: str, rate: str, pitch: str, text: str) -> Path:
    key = f"{voice}|{rate}|{pitch}|{text}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return _CACHE_DIR / f"{digest}.mp3"


async def _edge_tts_synth(text: str, voice: Optional[str]) -> str:
    import edge_tts

    v = voice or "zh-TW-YunJheNeural"
    rate = "-20%"
    pitch = "-15Hz"
    cache_path = _tts_cache_path(v, rate, pitch, text)
    if cache_path.exists():
        return str(cache_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    comm = edge_tts.Communicate(text=text, voice=v, rate=rate, pitch=pitch)
    await comm.save(str(cache_path))
    return str(cache_path)


def _default_synth_fn(text: str, voice: Optional[str]) -> str:
    import asyncio

    return asyncio.run(_edge_tts_synth(text, voice))


def build_overlays(
    clip: dict,
    candidates: dict,
    *,
    synth_fn: Callable[[str, Optional[str]], str] = _default_synth_fn,
    repo_root: Path = REPO_ROOT,
) -> List[Overlay]:
    marvin_audio = candidates.get("marvin_audio", "resynth")
    delay = float(candidates.get("marvin_delay", 0.0))
    clip_from = float(clip["from"])

    overlays: List[Overlay] = []
    for ev in clip.get("events", []):
        kind = ev.get("kind")
        if kind == "marvin":
            if marvin_audio != "resynth":
                continue
            path = synth_fn(ev.get("text", ""), ev.get("voice"))
            delay_s = float(ev["from"]) + delay - clip_from
            overlays.append(Overlay(path=path, delay_ms=max(0, round(delay_s * 1000)), af=TTS_LOUDNESS_AF))
        elif kind == "ack":
            if marvin_audio != "resynth":
                continue
            file_path = ev.get("file", "")
            p = Path(file_path)
            if not p.is_absolute():
                p = repo_root / file_path
            delay_s = float(ev["from"]) + delay - clip_from
            overlays.append(Overlay(path=str(p), delay_ms=max(0, round(delay_s * 1000)), af=TTS_LOUDNESS_AF))
        elif kind == "song_card":
            sfx = repo_root / "assets" / "dj_sfx" / "scratch.wav"
            if not sfx.exists():
                print(f"警告：找不到刷碟音效 {sfx}，略過", file=sys.stderr)
                continue
            delay_s = float(ev["from"]) - clip_from
            overlays.append(Overlay(path=str(sfx), delay_ms=max(0, round(delay_s * 1000)), af="volume=0.6"))

    return overlays


# ---------------------------------------------------------------------------
# build_mix_cmd
# ---------------------------------------------------------------------------

def build_mix_cmd(base_wav: str, overlays: Sequence[Overlay], out_wav: str, duration: float) -> List[str]:
    cmd = ["ffmpeg", "-y", "-i", base_wav]
    for ov in overlays:
        cmd += ["-i", ov.path]

    if not overlays:
        fc = "[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[out]"
    else:
        parts = []
        for i, ov in enumerate(overlays, 1):
            ms = ov.delay_ms
            parts.append(
                f"[{i}:a]aformat=sample_rates=48000:channel_layouts=stereo,{ov.af},"
                f"adelay={ms}|{ms}[o{i}];"
            )
        mix_inputs = "[0:a]" + "".join(f"[o{i}]" for i in range(1, len(overlays) + 1))
        parts.append(
            f"{mix_inputs}amix=inputs={len(overlays) + 1}:normalize=0:duration=first,"
            f"loudnorm=I=-14:TP=-1.5:LRA=11[out]"
        )
        fc = "".join(parts)

    cmd += [
        "-filter_complex", fc,
        "-map", "[out]",
        "-t", f"{duration:.3f}",
        "-ar", "48000", "-ac", "2",
        out_wav,
    ]
    return cmd


# ---------------------------------------------------------------------------
# DisplayItem / build_display_items
# ---------------------------------------------------------------------------

@dataclass
class DisplayItem:
    kind: str
    start: float
    end: float
    speaker: Optional[str] = None
    text: str = ""
    title: str = ""
    artist: str = ""


def _default_audio_len_fn(path: str) -> float:
    return _ffprobe_duration(path)


def build_display_items(
    clip: dict,
    *,
    delay: float,
    marvin_audio: str,
    audio_len_fn: Callable[[str], float] = _default_audio_len_fn,
    repo_root: Path = REPO_ROOT,
) -> List[DisplayItem]:
    clip_from = float(clip["from"])
    duration = float(clip["to"]) - clip_from

    raw_items: List[DisplayItem] = []
    for ev in clip.get("events", []):
        kind = ev.get("kind")
        if kind == "human":
            raw_items.append(DisplayItem(
                kind="human",
                start=float(ev["from"]) - clip_from,
                end=float(ev["to"]) - clip_from,
                speaker=ev.get("speaker"),
                text=ev.get("text", ""),
            ))
        elif kind == "marvin":
            start = float(ev["from"]) + delay - clip_from
            if marvin_audio == "resynth":
                length = audio_len_fn(ev.get("_audio_path", "")) + 0.3
            else:
                length = _clamp(len(ev.get("text", "")) / 4.0, 1.0, 20.0)
            raw_items.append(DisplayItem(
                kind="marvin" if ev.get("voice") is None else "marmo",
                start=start,
                end=start + length,
                speaker=None,
                text=ev.get("text", ""),
            ))
        elif kind == "ack":
            start = float(ev["from"]) + delay - clip_from
            file_path = ev.get("file", "")
            p = Path(file_path)
            if not p.is_absolute():
                p = repo_root / file_path
            length = audio_len_fn(str(p)) + 0.3
            raw_items.append(DisplayItem(
                kind="marvin",
                start=start,
                end=start + length,
                speaker=None,
                text=ev.get("text", ""),
            ))
        elif kind == "song_card":
            raw_items.append(DisplayItem(
                kind="song_card",
                start=float(ev["from"]) - clip_from,
                end=float(ev["to"]) - clip_from,
                title=ev.get("title", ""),
                artist=ev.get("artist", ""),
            ))

    expanded: List[DisplayItem] = []
    for item in raw_items:
        if item.kind == "song_card" or len(item.text) <= 24:
            expanded.append(item)
            continue
        segs = split_for_reading(item.text, 24)
        if not segs:
            expanded.append(item)
            continue
        total_chars = sum(len(s) for s in segs)
        total_len = item.end - item.start
        cur = item.start
        for seg in segs:
            portion = total_len * (len(seg) / total_chars) if total_chars else 0
            seg_end = cur + portion
            expanded.append(DisplayItem(
                kind=item.kind, start=cur, end=seg_end,
                speaker=item.speaker, text=seg,
            ))
            cur = seg_end

    result: List[DisplayItem] = []
    for item in expanded:
        start = _clamp(item.start, 0, duration)
        end = _clamp(item.end, 0, duration)
        if end - start <= 0:
            continue
        result.append(DisplayItem(
            kind=item.kind, start=start, end=end,
            speaker=item.speaker, text=item.text,
            title=item.title, artist=item.artist,
        ))

    return result


# ---------------------------------------------------------------------------
# assign_speakers
# ---------------------------------------------------------------------------

def assign_speakers(items: Sequence[DisplayItem]) -> Dict[str, dict]:
    speakers: Dict[str, dict] = {}
    human_order: List[str] = []

    for item in items:
        if item.kind == "human":
            name = item.speaker or ""
            if name not in human_order:
                human_order.append(name)

    for i, name in enumerate(human_order):
        speakers[name] = {
            "color": _PALETTE_COLORS[i % len(_PALETTE_COLORS)],
            "shape": _PALETTE_SHAPES[i % len(_PALETTE_SHAPES)],
            "index": i,
        }

    speakers["馬文"] = {"color": MARVIN_COLOR, "shape": "dot", "index": -1}
    speakers["Marmo"] = {"color": MARMO_COLOR, "shape": "star", "index": -1}

    return speakers


# ---------------------------------------------------------------------------
# segment_items
# ---------------------------------------------------------------------------

def segment_items(items: Sequence[DisplayItem], duration: float):
    sorted_items = sorted(items, key=lambda it: it.start)
    boundary_set = {0.0, duration}
    for it in sorted_items:
        boundary_set.add(_clamp(it.start, 0, duration))
        boundary_set.add(_clamp(it.end, 0, duration))
    boundaries = sorted(boundary_set)

    raw_segments = []
    for a, b in zip(boundaries, boundaries[1:]):
        if b - a <= 0.001:
            continue
        active = [it for it in sorted_items if it.start <= a < it.end]
        if len(active) > 4:
            active = active[-4:]
        raw_segments.append((a, b, active))

    merged: List[list] = []
    for a, b, active in raw_segments:
        if merged:
            prev_active = merged[-1][2]
            same = len(prev_active) == len(active) and all(
                x is y for x, y in zip(prev_active, active)
            )
            if same:
                merged[-1][1] = b
                continue
        merged.append([a, b, active])

    return [(a, b, active) for a, b, active in merged]


# ---------------------------------------------------------------------------
# avatars
# ---------------------------------------------------------------------------

def make_generic_avatar(color: str, shape: str, size: int = 96) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((0, 0, size - 1, size - 1), fill=color)

    cx, cy = size / 2, size / 2
    r = size * 0.28

    if shape == "dot":
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill="#FFFFFF")
    elif shape == "triangle":
        draw.polygon([(cx, cy - r), (cx - r, cy + r), (cx + r, cy + r)], fill="#FFFFFF")
    elif shape == "square":
        draw.rectangle((cx - r, cy - r, cx + r, cy + r), fill="#FFFFFF")
    elif shape == "diamond":
        draw.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], fill="#FFFFFF")
    elif shape == "star":
        import math
        points = []
        for i in range(10):
            ang = math.pi / 2 + i * math.pi / 5
            rr = r if i % 2 == 0 else r * 0.4
            points.append((cx + rr * math.cos(ang), cy - rr * math.sin(ang)))
        draw.polygon(points, fill="#FFFFFF")
    elif shape == "hexagon":
        import math
        points = []
        for i in range(6):
            ang = math.pi / 6 + i * math.pi / 3
            points.append((cx + r * math.cos(ang), cy - r * math.sin(ang)))
        draw.polygon(points, fill="#FFFFFF")
    else:
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill="#FFFFFF")

    return img


def load_marvin_avatar(path, size: int = 96) -> Image.Image:
    p = Path(path)
    if not p.exists():
        return make_generic_avatar(MARVIN_COLOR, "dot", size=size)

    img = Image.open(p).convert("RGBA")
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img = img.resize((size, size), Image.LANCZOS)

    mask = Image.new("L", (size, size), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.ellipse((0, 0, size - 1, size - 1), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


# ---------------------------------------------------------------------------
# render_frame
# ---------------------------------------------------------------------------

def _avatar_for(item: DisplayItem, speakers: Dict[str, dict], marvin_avatar_img: Image.Image) -> Image.Image:
    if item.kind == "marvin":
        return marvin_avatar_img
    if item.kind == "marmo":
        return make_generic_avatar(MARMO_COLOR, "star")
    info = speakers.get(item.speaker or "", {"color": "#FFFFFF", "shape": "dot"})
    return make_generic_avatar(info["color"], info["shape"])


def _color_for(item: DisplayItem, speakers: Dict[str, dict]) -> str:
    if item.kind == "marvin":
        return MARVIN_COLOR
    if item.kind == "marmo":
        return MARMO_COLOR
    return speakers.get(item.speaker or "", {"color": "#FFFFFF"})["color"]


def render_frame(
    active: Sequence[DisplayItem],
    speakers: Dict[str, dict],
    font,
    *,
    marvin_avatar_img: Optional[Image.Image] = None,
    size=(1080, 1920),
) -> Image.Image:
    img = Image.new("RGB", size, (0, 0, 0))
    if not active:
        return img

    if marvin_avatar_img is None:
        marvin_avatar_img = make_generic_avatar(MARVIN_COLOR, "dot")

    draw = ImageDraw.Draw(img)
    max_width = 800
    line_height = font.size * 1.3
    gap = font.size * 0.6
    avatar_size = 96

    blocks = []
    for item in active:
        if item.kind == "song_card":
            # 兩行字（0.6 倍小字＋正常字）＋上下留白，不可小於頭像高
            block_h = max(avatar_size, int(font.size * 0.6) + font.size + 40)
            blocks.append(("song_card", item, block_h))
            continue
        lines = wrap_text(item.text, font, max_width)
        block_h = max(avatar_size, len(lines) * line_height)
        blocks.append(("text", (item, lines), block_h))

    total_height = sum(h for _, _, h in blocks) + gap * (len(blocks) - 1)
    y = (size[1] - total_height) / 2

    for kind, payload, block_h in blocks:
        if kind == "song_card":
            item = payload
            x0, y0, x1, y1 = 80, y, 1000, y + block_h
            draw.rounded_rectangle((x0, y0, x1, y1), radius=24, fill="#1E1E1E")
            small_font = ImageFont.truetype(font.path, max(10, int(font.size * 0.6)))
            label_text = "正在播放"
            draw.text((x0 + 24, y0 + 10), label_text, font=small_font, fill="#AAAAAA")
            title_line = f"《{item.title}》" + (f"— {item.artist}" if item.artist else "")
            draw.text((x0 + 24, y0 + 10 + small_font.size + 6), title_line, font=font, fill="#FFFFFF")
        else:
            item, lines = payload
            avatar = _avatar_for(item, speakers, marvin_avatar_img)
            img.paste(avatar, (80, int(y)), avatar)
            color = _color_for(item, speakers)
            ty = y
            for line in lines:
                draw.text((200, ty), line, font=font, fill=color)
                ty += font.size * 1.3
        y += block_h + gap

    return img


# ---------------------------------------------------------------------------
# render_index_md
# ---------------------------------------------------------------------------

def render_index_md(rec_start: str, entries: List[dict]) -> str:
    lines = [f"# Marvin 推廣素材（{rec_start} 錄音）", ""]
    lines.append("| 編號 | 標題 | 類型 | 長度 | 檔案 |")
    lines.append("|---|---|---|---|---|")
    for e in entries:
        lines.append(f"| {e['id']} | {e['title']} | {e['post_type']} | {e['length_s']} 秒 | {e['id']}.mp4 |")

    for e in entries:
        lines.append("")
        lines.append(f"## {e['id']}｜{e['title']}")
        for line in e.get("lines", []):
            lines.append(f"- {line}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# rendering pipeline
# ---------------------------------------------------------------------------

def _human_label(index: int) -> str:
    if index < 26:
        return f"成員{chr(ord('A') + index)}"
    return f"成員{index + 1}"


def _render_clip(
    clip: dict,
    candidates: dict,
    out_dir: Path,
    *,
    synth_fn: Callable[[str, Optional[str]], str],
    repo_root: Path,
    font_path: str,
    font_size: int,
    marvin_avatar_path: Path,
) -> dict:
    recording = candidates["recording"]
    marvin_audio = candidates.get("marvin_audio", "resynth")
    delay = float(candidates.get("marvin_delay", 0.0))
    clip_from = float(clip["from"])
    clip_to = float(clip["to"])
    duration = clip_to - clip_from
    clip_id = clip["id"]

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        base_wav = tdp / "base.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{clip_from}", "-t", f"{duration}",
             "-i", str(recording), "-vn", "-ac", "2", "-ar", "48000", str(base_wav)],
            check=True, capture_output=True,
        )

        overlays = build_overlays(clip, candidates, synth_fn=synth_fn, repo_root=repo_root)

        synth_paths: Dict[int, str] = {}
        for i, ev in enumerate(clip.get("events", [])):
            if ev.get("kind") == "marvin" and marvin_audio == "resynth":
                ev = dict(ev)
                ev["_audio_path"] = synth_fn(ev.get("text", ""), ev.get("voice"))
                clip["events"][i] = ev

        mixed_wav = tdp / "mixed.wav"
        mix_cmd = build_mix_cmd(str(base_wav), overlays, str(mixed_wav), duration)
        subprocess.run(mix_cmd, check=True, capture_output=True)

        def audio_len_fn(path: str) -> float:
            if not path:
                return 0.0
            try:
                return _ffprobe_duration(path)
            except Exception:
                return 0.0

        items = build_display_items(
            clip, delay=delay, marvin_audio=marvin_audio,
            audio_len_fn=audio_len_fn, repo_root=repo_root,
        )
        speakers = assign_speakers(items)
        font = ImageFont.truetype(font_path, font_size)
        marvin_avatar_img = load_marvin_avatar(marvin_avatar_path)

        segments = segment_items(items, duration)
        seg_files = []
        for i, (a, b, active) in enumerate(segments):
            img = render_frame(active, speakers, font, marvin_avatar_img=marvin_avatar_img)
            fname = f"seg{i:04d}.png"
            img.save(str(tdp / fname))
            seg_files.append((fname, b - a))

        concat_path = tdp / "concat.txt"
        with concat_path.open("w", encoding="utf-8") as f:
            for fname, dur in seg_files:
                f.write(f"file '{fname}'\n")
                f.write(f"duration {dur}\n")
            if seg_files:
                f.write(f"file '{seg_files[-1][0]}'\n")

        out_path = (out_dir / f"{clip_id}.mp4").resolve()  # ffmpeg 在暫存目錄跑（cwd=td），相對路徑會落錯地方
        cmd = build_ffmpeg_cmd(str(concat_path), str(mixed_wav), str(out_path))
        subprocess.run(cmd, check=True, cwd=td, capture_output=True)

    lines = index_lines(clip, speakers)
    return {
        "id": clip_id,
        "title": clip.get("title", ""),
        "post_type": clip.get("post_type", ""),
        "length_s": round(duration),
        "lines": lines,
    }


def index_lines(clip: dict, speakers: Dict[str, dict]) -> List[str]:
    """素材目錄用的字幕全文：依事件時間順序；human 一律換成「成員X」（與頭像編號對應），不寫原名。"""
    lines = []
    for ev in sorted(clip.get("events", []), key=lambda e: float(e.get("from", 0))):
        kind = ev.get("kind")
        if kind == "human":
            idx = speakers.get(ev.get("speaker") or "", {}).get("index", 0)
            lines.append(f"{_human_label(idx)}：{ev.get('text', '')}")
        elif kind in ("marvin", "ack"):
            speaker_name = "Marmo" if kind == "marvin" and ev.get("voice") is not None else "馬文"
            text = ev.get("text", "")
            if text:
                lines.append(f"{speaker_name}：{text}")
        elif kind == "song_card":
            title = ev.get("title", "")
            artist = ev.get("artist", "")
            suffix = f"— {artist}" if artist else ""
            lines.append(f"🎵 正在播放：《{title}》{suffix}")
    return lines


def render_candidates(
    candidates: dict,
    out_dir: Path,
    *,
    all_clips: bool = False,
    synth_fn: Callable[[str, Optional[str]], str] = _default_synth_fn,
    repo_root: Path = REPO_ROOT,
    font_path: Optional[str] = None,
    font_size: int = 60,
    marvin_avatar: Optional[Path] = None,
) -> List[dict]:
    font_path = font_path or _default_font()
    if not font_path:
        raise RuntimeError("找不到預設中文字型，請用 --font 指定字型路徑。")
    marvin_avatar_path = Path(marvin_avatar) if marvin_avatar else repo_root / "assets" / "marvin_avatar.png"

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    clips = candidates.get("clips", [])
    if not all_clips:
        clips = [c for c in clips if c.get("approved")]

    entries = []
    for clip in clips:
        entry = _render_clip(
            clip, candidates, out_dir,
            synth_fn=synth_fn, repo_root=repo_root,
            font_path=font_path, font_size=font_size,
            marvin_avatar_path=marvin_avatar_path,
        )
        entries.append(entry)

    index_md = render_index_md(candidates.get("rec_start", ""), entries)
    (out_dir / "index.md").write_text(index_md, encoding="utf-8")

    return entries


# ---------------------------------------------------------------------------
# fetch_avatar
# ---------------------------------------------------------------------------

def fetch_avatar(token: str, out_path, *, urlopen=urllib.request.urlopen) -> None:
    req = urllib.request.Request(
        "https://discord.com/api/v10/users/@me",
        headers={"Authorization": f"Bot {token}"},
    )
    with urlopen(req) as resp:
        data = json.loads(resp.read())

    user_id = data["id"]
    avatar_hash = data["avatar"]
    cdn_url = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.png?size=256"
    cdn_req = urllib.request.Request(cdn_url)
    with urlopen(cdn_req) as resp:
        png_bytes = resp.read()

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(png_bytes)


def _do_fetch_avatar(args) -> int:
    from dotenv import load_dotenv
    import os

    load_dotenv(REPO_ROOT / ".env")
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print("錯誤：找不到 DISCORD_BOT_TOKEN（.env）。", file=sys.stderr)
        return 2

    out_path = Path(args.output) if args.output else REPO_ROOT / "assets" / "marvin_avatar.png"
    fetch_avatar(token, out_path)
    print(f"已下載頭像到 {out_path}")
    return 0


def _do_render(args) -> int:
    candidates_path = Path(args.candidates)
    candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
    out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_OUT_DIR
    marvin_avatar = Path(args.marvin_avatar) if args.marvin_avatar else None

    entries = render_candidates(
        candidates, out_dir,
        all_clips=args.all,
        font_path=args.font,
        font_size=args.font_size,
        marvin_avatar=marvin_avatar,
    )
    print(f"共輸出 {len(entries)} 支影片到 {out_dir}")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="推廣素材批次出片")
    sub = parser.add_subparsers(dest="command", required=True)

    p_render = sub.add_parser("render")
    p_render.add_argument("candidates")
    p_render.add_argument("--out-dir")
    p_render.add_argument("--all", action="store_true")
    p_render.add_argument("--font")
    p_render.add_argument("--font-size", type=int, default=60)
    p_render.add_argument("--marvin-avatar")

    p_avatar = sub.add_parser("fetch-avatar")
    p_avatar.add_argument("-o", "--output")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.command == "render":
        return _do_render(args)
    if args.command == "fetch-avatar":
        return _do_fetch_avatar(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""錄音 → 分說話者上色字幕 → 黑底直式影片。

離線工具，兩個子指令：
  prepare  從 marvin.db 逐字稿 + marvin_speech.log 撈出對話，產生 .srt 草稿
  render   把校對過的 .srt 燒成黑底直式（1080x1920）mp4，字幕用 Pillow 逐幀畫成 PNG

對資料只讀不寫：sqlite 一律 `mode=ro` 開啟。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent

_FIXED_COLORS = {
    "馬文": "#4FC3F7",
    "Marmo": "#FFB74D",
    "": "#FFFFFF",
}
_PALETTE = [
    "#FFF176", "#F48FB1", "#A5D6A7", "#CE93D8",
    "#FFAB91", "#80DEEA", "#E6EE9C", "#BCAAA4",
]

_FONT_CANDIDATES = [
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
]


@dataclass
class Cue:
    start: float
    end: float
    label: str
    text: str


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def parse_obs_start(filename: str) -> Optional[datetime]:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2}) (\d{2})-(\d{2})-(\d{2})", str(filename))
    if not m:
        return None
    y, mo, d, h, mi, s = (int(g) for g in m.groups())
    try:
        return datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None


def human_cue_window(end_ts: float, text: str) -> Tuple[float, float]:
    dur = _clamp(len(text) / 4.5, 0.8, 8.0)
    return (end_ts - dur, end_ts + 0.8)


def marvin_cue_window(start_ts: float, text: str) -> Tuple[float, float]:
    dur = _clamp(len(text) / 4.0, 1.0, 20.0)
    return (start_ts, start_ts + dur)


def read_speech_log(dir_path) -> List[dict]:
    base = Path(dir_path) / "marvin_speech.log"
    candidates = [base] + [base.with_name(base.name + f".{i}") for i in (1, 2, 3)]
    rows: List[dict] = []
    for path in candidates:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _member_label(idx: int) -> str:
    if idx < 26:
        return f"成員{chr(ord('A') + idx)}"
    return f"成員{idx + 1}"


def build_cues(
    human_rows,
    marvin_rows,
    *,
    rec_start: float,
    duration: float,
    offset: float,
    anonymize: bool,
    aliases: Dict[str, str],
    include_marvin: bool,
) -> List[Cue]:
    label_map: Dict[str, str] = {}

    def get_label(name: str) -> str:
        if name in aliases:
            return aliases[name]
        if not anonymize:
            return name
        if name not in label_map:
            label_map[name] = _member_label(len(label_map))
        return label_map[name]

    cues: List[Cue] = []

    for name, text, ts in human_rows:
        text = (text or "").strip()
        if not text:
            continue
        start_abs, end_abs = human_cue_window(ts, text)
        start = start_abs - rec_start + offset
        end = end_abs - rec_start + offset
        if end <= 0 or start >= duration:
            continue
        start = _clamp(start, 0, duration)
        end = _clamp(end, 0, duration)
        cues.append(Cue(start=start, end=end, label=get_label(name), text=text))

    if include_marvin:
        for row in marvin_rows:
            text = (row.get("text") or "").strip()
            if not text:
                continue
            voice = row.get("voice")
            label = "馬文" if voice is None else "Marmo"
            start_abs, end_abs = marvin_cue_window(row["start"], text)
            start = start_abs - rec_start + offset
            end = end_abs - rec_start + offset
            if end <= 0 or start >= duration:
                continue
            start = _clamp(start, 0, duration)
            end = _clamp(end, 0, duration)
            cues.append(Cue(start=start, end=end, label=label, text=text))

    cues.sort(key=lambda c: c.start)

    last_index_by_label: Dict[str, int] = {}
    for i, c in enumerate(cues):
        prev_i = last_index_by_label.get(c.label)
        if prev_i is not None and cues[prev_i].end > c.start:
            cues[prev_i].end = c.start
        last_index_by_label[c.label] = i

    return cues


def _format_srt_time(seconds: float) -> str:
    total_ms = round(seconds * 1000)
    h = total_ms // 3600000
    m = (total_ms % 3600000) // 60000
    s = (total_ms % 60000) // 1000
    ms = total_ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_srt(cues: Sequence[Cue]) -> str:
    lines = []
    for i, c in enumerate(cues, 1):
        lines.append(str(i))
        lines.append(f"{_format_srt_time(c.start)} --> {_format_srt_time(c.end)}")
        display = f"{c.label}：{c.text}" if c.label else c.text
        lines.append(display)
        lines.append("")
    return "\n".join(lines)


_TIME_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)


def _parse_srt_time(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_srt(content: str) -> List[Cue]:
    blocks = re.split(r"\n\s*\n", content.strip())
    cues: List[Cue] = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        lines = block.splitlines()
        idx = 0
        if lines and lines[0].strip().isdigit():
            idx = 1
        if idx >= len(lines):
            continue
        m = _TIME_RE.search(lines[idx])
        if not m:
            continue
        start = _parse_srt_time(*m.groups()[0:4])
        end = _parse_srt_time(*m.groups()[4:8])
        text_lines = lines[idx + 1:]
        if not text_lines:
            cues.append(Cue(start=start, end=end, label="", text=""))
            continue
        first_line = text_lines[0]
        sep = re.search(r"[:：]", first_line)
        if sep and sep.start() <= 20:
            label = first_line[: sep.start()]
            first_rest = first_line[sep.start() + 1:]
            text = "\n".join([first_rest] + text_lines[1:])
        else:
            label = ""
            text = "\n".join(text_lines)
        cues.append(Cue(start=start, end=end, label=label, text=text))
    return cues


def assign_colors(cues: Sequence[Cue]) -> Dict[str, str]:
    colors: Dict[str, str] = {}
    palette_idx = 0
    for c in cues:
        if c.label in colors:
            continue
        if c.label in _FIXED_COLORS:
            colors[c.label] = _FIXED_COLORS[c.label]
        else:
            colors[c.label] = _PALETTE[palette_idx % len(_PALETTE)]
            palette_idx += 1
    return colors


def segment_timeline(cues: Sequence[Cue], duration: float) -> List[Tuple[float, float, List[Cue]]]:
    sorted_cues = sorted(cues, key=lambda c: c.start)
    boundary_set = {0.0, duration}
    for c in sorted_cues:
        boundary_set.add(_clamp(c.start, 0, duration))
        boundary_set.add(_clamp(c.end, 0, duration))
    boundaries = sorted(boundary_set)

    raw_segments: List[Tuple[float, float, List[Cue]]] = []
    for a, b in zip(boundaries, boundaries[1:]):
        if b - a <= 0.001:
            continue
        active = [c for c in sorted_cues if c.start <= a < c.end]
        if len(active) > 4:
            active = active[-4:]
        raw_segments.append((a, b, active))

    merged: List[List] = []
    for seg in raw_segments:
        a, b, active = seg
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


def wrap_text(text: str, font, max_width: float) -> List[str]:
    lines: List[str] = []
    for paragraph in text.split("\n"):
        cur = ""
        for ch in paragraph:
            candidate = cur + ch
            if not cur or font.getlength(candidate) <= max_width:
                cur = candidate
            else:
                lines.append(cur)
                cur = ch
        lines.append(cur)
    return lines


def render_frame(active: Sequence[Cue], colors: Dict[str, str], font, size=(1080, 1920)) -> Image.Image:
    img = Image.new("RGB", size, (0, 0, 0))
    if not active:
        return img
    draw = ImageDraw.Draw(img)
    max_width = size[0] - 160
    line_height = font.size * 1.3
    gap = font.size * 0.6

    blocks = []
    for c in active:
        display = f"{c.label}：{c.text}" if c.label else c.text
        lines = wrap_text(display, font, max_width)
        color = colors.get(c.label, "#FFFFFF")
        blocks.append((lines, color))

    total_height = sum(len(lines) * line_height for lines, _ in blocks) + gap * (len(blocks) - 1)
    y = (size[1] - total_height) / 2
    for lines, color in blocks:
        for line in lines:
            w = font.getlength(line)
            x = (size[0] - w) / 2
            draw.text((x, y), line, font=font, fill=color)
            y += line_height
        y += gap

    return img


def build_ffmpeg_cmd(concat_list, recording, out_path) -> List[str]:
    return [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-i", str(recording),
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf", "fps=30,format=yuv420p",
        "-c:v", "libx264", "-preset", "medium", "-crf", "23",
        "-c:a", "aac", "-b:a", "160k",
        "-shortest", "-movflags", "+faststart", str(out_path),
    ]


def _ffprobe_duration(path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def _default_font() -> Optional[str]:
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


def _do_render(recording: str, srt_path: str, out_path: str, font_path: str, font_size: int) -> int:
    content = Path(srt_path).read_text(encoding="utf-8")
    cues = parse_srt(content)
    colors = assign_colors(cues)
    duration = _ffprobe_duration(recording)
    segments = segment_timeline(cues, duration)
    font = ImageFont.truetype(font_path, font_size)

    with tempfile.TemporaryDirectory() as td:
        seg_files: List[Tuple[str, float]] = []
        for i, (a, b, active) in enumerate(segments):
            img = render_frame(active, colors, font)
            fname = f"seg{i:04d}.png"
            img.save(str(Path(td) / fname))
            seg_files.append((fname, b - a))

        concat_path = Path(td) / "concat.txt"
        with concat_path.open("w", encoding="utf-8") as f:
            for fname, dur in seg_files:
                f.write(f"file '{fname}'\n")
                f.write(f"duration {dur}\n")
            if seg_files:
                f.write(f"file '{seg_files[-1][0]}'\n")

        cmd = build_ffmpeg_cmd(str(concat_path), str(Path(recording).resolve()), str(Path(out_path).resolve()))
        subprocess.run(cmd, check=True, cwd=td)

    return 0


def _parse_aliases(raw: Sequence[str]) -> Dict[str, str]:
    aliases = {}
    for item in raw or []:
        if "=" not in item:
            continue
        name, display = item.split("=", 1)
        aliases[name] = display
    return aliases


def _do_prepare(args) -> int:
    recording = Path(args.recording)

    rec_start_dt: Optional[datetime] = None
    if args.start:
        rec_start_dt = datetime.strptime(args.start, "%Y-%m-%d %H:%M:%S")
    else:
        rec_start_dt = parse_obs_start(recording.name) or parse_obs_start(str(recording))

    if rec_start_dt is None:
        print("錯誤：無法判斷錄音開始時間，請用 --start \"YYYY-MM-DD HH:MM:SS\" 指定。", file=sys.stderr)
        return 2

    rec_start = rec_start_dt.timestamp()
    duration = _ffprobe_duration(recording)

    db_path = Path(args.db) if args.db else REPO_ROOT / "marvin.db"
    speech_log_dir = Path(args.speech_log_dir) if args.speech_log_dir else REPO_ROOT

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = conn.execute(
            "SELECT speaker, text, timestamp FROM transcripts "
            "WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp",
            (rec_start - 10, rec_start + duration + 10),
        )
        human_rows = list(cur.fetchall())
    finally:
        conn.close()

    include_marvin = not args.no_marvin
    marvin_rows: List[dict] = []
    if include_marvin:
        marvin_rows = read_speech_log(speech_log_dir)
        if not marvin_rows:
            print("提示：找不到 marvin_speech.log，將不含馬文台詞。")

    aliases = _parse_aliases(args.alias)

    cues = build_cues(
        human_rows,
        marvin_rows,
        rec_start=rec_start,
        duration=duration,
        offset=args.offset,
        anonymize=not args.real_names,
        aliases=aliases,
        include_marvin=include_marvin,
    )

    out_path = Path(args.output) if args.output else recording.with_suffix(".srt")
    out_path.write_text(format_srt(cues), encoding="utf-8")

    per_speaker: Dict[str, int] = {}
    for c in cues:
        per_speaker[c.label] = per_speaker.get(c.label, 0) + 1

    print(f"共 {len(cues)} 則字幕，輸出到 {out_path}")
    for label, count in per_speaker.items():
        print(f"  {label}: {count} 句")
    print(f"改好後執行：python {Path(__file__).name} render \"{recording}\" \"{out_path}\"")
    print("提醒：人類字幕的開始時間是依字數往回推估，請實際播放核對。")

    return 0


def _do_render_cmd(args) -> int:
    font_path = args.font or _default_font()
    if not font_path:
        print("錯誤：找不到預設中文字型，請用 --font 指定字型路徑。", file=sys.stderr)
        return 2

    recording = Path(args.recording)
    out_path = Path(args.output) if args.output else recording.with_name(recording.stem + "_vertical.mp4")

    return _do_render(str(recording), args.subs, str(out_path), font_path, args.font_size)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="錄音 → 分說話者上色字幕 → 黑底直式影片")
    sub = parser.add_subparsers(dest="command", required=True)

    p_prepare = sub.add_parser("prepare")
    p_prepare.add_argument("recording")
    p_prepare.add_argument("--start")
    p_prepare.add_argument("--offset", type=float, default=0.0)
    p_prepare.add_argument("--db")
    p_prepare.add_argument("--speech-log-dir")
    p_prepare.add_argument("--real-names", action="store_true")
    p_prepare.add_argument("--alias", action="append", default=[])
    p_prepare.add_argument("--no-marvin", action="store_true")
    p_prepare.add_argument("-o", "--output")

    p_render = sub.add_parser("render")
    p_render.add_argument("recording")
    p_render.add_argument("subs")
    p_render.add_argument("-o", "--output")
    p_render.add_argument("--font")
    p_render.add_argument("--font-size", type=int, default=64)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.command == "prepare":
        return _do_prepare(args)
    if args.command == "render":
        return _do_render_cmd(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

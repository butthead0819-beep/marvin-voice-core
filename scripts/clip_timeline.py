"""推廣素材：時間軸輸出 / 候選清單審閱稿 / 候選檔驗證。

流程：錄音 →（timeline）產出時間軸 → 人工／Claude 讀時間軸挑候選片段寫成
candidates.json →（review）產出給人讀的審閱稿 → 人審 →（另一個工具 clip_render）
批次出片。

對資料只讀不寫：sqlite 一律 `mode=ro` 開啟。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from scripts.make_subtitle_video import (
    _ffprobe_duration,
    human_cue_window,
    parse_obs_start,
    read_speech_log,
)

import ack_templates

REPO_ROOT = Path(__file__).resolve().parent.parent

_VALID_KINDS = {"human", "marvin", "ack", "song_card"}

SONG_CARD_SECONDS = 4.0  # 歌名卡預設顯示秒數

DEFAULT_OBS_LOG_DIR = Path.home() / "Library" / "Application Support" / "obs-studio" / "logs"

_BOT_LOG_ACK_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}) \[INFO\] "
    r"cogs\.voice_controller: 🗣️ \[Ack:[^\]]+\] 播放 (\S+\.mp3)\s*$"
)

_OBS_LOG_NAME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2}) (\d{2})-(\d{2})-(\d{2})")

_OBS_WRITING_FILE_RE = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2})\.(\d{3}): .*Writing file '(.+)'"
)


def find_obs_recording_start(recording: Path, logs_dir: Path) -> Optional[datetime]:
    """從 OBS log 找出錄音檔真正開始的毫秒時間（檔名只到整秒）。"""
    if not logs_dir.is_dir():
        return None

    log_files = sorted(logs_dir.glob("*.txt"), key=lambda p: p.name, reverse=True)
    for log_path in log_files:
        m = _OBS_LOG_NAME_RE.search(log_path.name)
        if not m:
            continue
        y, mo, d, h, mi, s = (int(g) for g in m.groups())
        try:
            log_dt = datetime(y, mo, d, h, mi, s)
        except ValueError:
            continue

        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            wm = _OBS_WRITING_FILE_RE.match(line)
            if not wm:
                continue
            h2, mi2, s2, ms2, file_str = wm.groups()
            if Path(file_str).name != recording.name:
                continue
            result = datetime.combine(log_dt.date(), time(int(h2), int(mi2), int(s2), int(ms2) * 1000))
            if result < log_dt:
                from datetime import timedelta
                result += timedelta(days=1)
            return result

    return None


# ---------------------------------------------------------------------------
# Ack lookups
# ---------------------------------------------------------------------------

def ack_text(filename: str) -> Optional[str]:
    for pool in ack_templates.POOLS.values():
        for text, fname in pool.items:
            if fname == filename:
                return text
    return None


def find_ack_path(filename: str) -> Optional[str]:
    for pool in ack_templates.POOLS.values():
        for _text, fname in pool.items:
            if fname == filename:
                return f"{pool.directory}/{filename}"
    return None


# ---------------------------------------------------------------------------
# bot_main.log 舊 ack 補抓
# ---------------------------------------------------------------------------

def parse_bot_log_acks(lines: Sequence[str], tz_local: bool = True) -> List[dict]:
    from datetime import datetime, timezone

    out: List[dict] = []
    for line in lines:
        m = _BOT_LOG_ACK_RE.match(line.rstrip("\n"))
        if not m:
            continue
        ts_str, ms_str, filename = m.groups()
        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        if tz_local:
            epoch = dt.timestamp()
        else:
            epoch = dt.replace(tzinfo=timezone.utc).timestamp()
        epoch += int(ms_str) / 1000.0
        out.append({"start": epoch, "file": filename})
    return out


def _read_log_rotations(dir_path, base_name: str, count: int) -> List[str]:
    base = Path(dir_path) / base_name
    candidates = [base] + [base.with_name(base.name + f".{i}") for i in range(1, count + 1)]
    lines: List[str] = []
    for path in candidates:
        if not path.exists():
            continue
        lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
    return lines


# ---------------------------------------------------------------------------
# 事件收集
# ---------------------------------------------------------------------------

def collect_events(human_rows, speech_rows, bot_acks, *, rec_start: float, duration: float) -> List[dict]:
    events: List[dict] = []

    seen_human: set = set()
    for speaker, text, ts in human_rows:
        text = (text or "").strip()
        if not text:
            continue
        dedup_key = (speaker, text, ts)
        if dedup_key in seen_human:
            continue
        seen_human.add(dedup_key)
        start_abs, end_abs = human_cue_window(ts, text)
        start = start_abs - rec_start
        end = end_abs - rec_start
        if end < 0 or start > duration:
            continue
        events.append({"kind": "human", "speaker": speaker, "from": start, "to": end, "text": text})

    speech_ack_seen: List[tuple] = []
    for row in speech_rows:
        if row.get("origin", "discord") != "discord":
            continue
        text = (row.get("text") or "").strip()
        if not text:
            continue
        start_abs = row.get("start")
        rel = start_abs - rec_start
        if rel < 0 or rel > duration:
            continue
        src = row.get("src")
        if src == "song":
            events.append({
                "kind": "song_card",
                "from": rel,
                "to": rel + SONG_CARD_SECONDS,
                "title": text,
                "artist": row.get("artist"),
            })
            continue
        if src == "ack":
            file = row.get("file")
            events.append({"kind": "ack", "file": file, "from": rel, "text": text})
            if file:
                speech_ack_seen.append((Path(file).name, start_abs))
        else:
            events.append({
                "kind": "marvin",
                "voice": row.get("voice"),
                "from": rel,
                "text": text,
                "src": src,
            })

    for ba in bot_acks:
        filename = ba["file"]
        start_abs = ba["start"]
        if any(filename == seen_name and abs(start_abs - seen_ts) <= 2.0 for seen_name, seen_ts in speech_ack_seen):
            continue
        rel = start_abs - rec_start
        if rel < 0 or rel > duration:
            continue
        events.append({
            "kind": "ack",
            "file": find_ack_path(filename),
            "from": rel,
            "text": ack_text(filename),
        })

    events.sort(key=lambda e: e["from"])
    return events


# ---------------------------------------------------------------------------
# 格式化
# ---------------------------------------------------------------------------

def _mmss(seconds: float) -> str:
    total = int(round(seconds))
    m, s = divmod(total, 60)
    return f"{m:02d}:{s:02d}"


def _mmss_deci(seconds: float) -> str:
    total_deci = int(round(seconds * 10))
    m, rem = divmod(total_deci, 600)
    s = rem / 10
    return f"{m:02d}:{s:04.1f}"


def _timeline_event_line(ev: dict) -> str:
    kind = ev["kind"]
    t = _mmss_deci(ev["from"])
    if kind == "human":
        return f"{t}  人  {ev['speaker']}：{ev['text']}"
    if kind == "marvin":
        name = "馬文" if ev.get("voice") is None else "Marmo"
        category = name + ("(DJ)" if ev.get("src") == "dj" else "")
        return f"{t}  {category}  {name}：{ev['text']}"
    if kind == "ack":
        return f"{t}  罐頭  馬文：{ev['text']}"
    if kind == "song_card":
        return f"{t}  歌曲  🎵《{ev['title']}》" + (f"— {ev['artist']}" if ev.get("artist") else "")
    raise ValueError(f"format_timeline 不支援的 kind：{kind}")


def format_timeline(events: Sequence[dict], *, rec_start_str: str, duration: float) -> str:
    lines = [f"# 錄音開始 {rec_start_str}  長度 {_mmss(duration)}", "# 時間皆為錄音內相對時間"]
    for ev in events:
        lines.append(_timeline_event_line(ev))
    return "\n".join(lines) + "\n"


def _format_review_event(ev: dict) -> str:
    t = _mmss(ev["from"])
    kind = ev["kind"]
    if kind == "human":
        return f"- {t} 🧑{ev['speaker']}：{ev['text']}"
    if kind == "marvin":
        name = "馬文" if ev.get("voice") is None else "Marmo"
        return f"- {t} 🤖{name}：{ev['text']}"
    if kind == "ack":
        return f"- {t} 🤖馬文（罐頭）：{ev['text']}"
    if kind == "song_card":
        artist = ev.get("artist")
        return f"- {t} 🎵正在播放：《{ev['title']}》" + (f"— {artist}" if artist else "")
    raise ValueError(f"render_review_md 不支援的 kind：{kind}")


def render_review_md(candidates: dict) -> str:
    clips = candidates.get("clips", [])
    lines = [
        f"# 素材候選審閱（{candidates.get('rec_start')}，共 {len(clips)} 段）",
        "審閱方式：看完回覆要用的編號；想改字幕直接寫在回覆裡。",
    ]
    for clip in clips:
        lines.append("")
        dur = int(round(clip["to"] - clip["from"]))
        header = (
            f"## {clip['id']}｜{clip['title']}（{clip['post_type']}，{dur} 秒，"
            f"{_mmss(clip['from'])}–{_mmss(clip['to'])}）"
        )
        if clip.get("approved"):
            header += " ✅"
        lines.append(header)
        if clip.get("note"):
            lines.append(clip["note"])
        for ev in clip.get("events", []):
            lines.append(_format_review_event(ev))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 驗證
# ---------------------------------------------------------------------------

def validate_candidates(candidates: dict, repo_root: Path) -> List[str]:
    errors: List[str] = []

    audio = candidates.get("marvin_audio")
    if audio not in ("resynth", "original"):
        errors.append(f"marvin_audio 不合法：{audio!r}")

    delay = candidates.get("marvin_delay")
    if not isinstance(delay, (int, float)) or isinstance(delay, bool) or delay < 0:
        errors.append(f"marvin_delay 不合法：{delay!r}")

    seen_ids: set = set()
    for clip in candidates.get("clips", []):
        cid = clip.get("id")
        if cid in seen_ids:
            errors.append(f"[{cid}] id 重複")
        else:
            seen_ids.add(cid)

        c_from = clip.get("from")
        c_to = clip.get("to")
        range_ok = c_from is not None and c_to is not None and c_from < c_to
        if not range_ok:
            errors.append(f"[{cid}] from>=to（{c_from}>={c_to}）")
        elif c_to - c_from > 90:
            errors.append(f"[{cid}] 長度超過 90 秒（{c_to - c_from}）")

        for ev in clip.get("events", []):
            kind = ev.get("kind")
            ev_from = ev.get("from")
            if range_ok and not (c_from <= ev_from < c_to):
                errors.append(f"[{cid}] 事件 from 超出範圍：{ev_from}")

            if kind == "human":
                if not ev.get("speaker"):
                    errors.append(f"[{cid}] human 事件缺 speaker")
                if not ev.get("text"):
                    errors.append(f"[{cid}] human 事件缺 text")
                if not (ev.get("to", ev_from) > ev_from):
                    errors.append(f"[{cid}] human 事件 to<=from")
            elif kind == "marvin":
                if not ev.get("text"):
                    errors.append(f"[{cid}] marvin 事件缺 text")
            elif kind == "ack":
                file = ev.get("file")
                resolved = None
                if file:
                    p = Path(file)
                    resolved = p if p.is_absolute() else (repo_root / file)
                if not file or not resolved.exists():
                    errors.append(f"[{cid}] ack 檔案不存在：{file}")
            elif kind == "song_card":
                if not ev.get("title"):
                    errors.append(f"[{cid}] song_card 事件缺 title")
                if not (ev.get("to", ev_from) > ev_from):
                    errors.append(f"[{cid}] song_card 事件 to<=from")
            else:
                errors.append(f"[{cid}] 不明 kind：{kind}")

    return errors


# ---------------------------------------------------------------------------
# 子指令
# ---------------------------------------------------------------------------

def _do_timeline(args) -> int:
    recording = Path(args.recording)

    rec_start_dt = None
    rec_start_source = None
    if args.start:
        rec_start_dt = datetime.strptime(args.start, "%Y-%m-%d %H:%M:%S")
        rec_start_source = "manual"
    else:
        obs_log_dir = Path(args.obs_log_dir) if args.obs_log_dir else DEFAULT_OBS_LOG_DIR
        rec_start_dt = find_obs_recording_start(recording, obs_log_dir)
        if rec_start_dt is not None:
            rec_start_source = "obs_log"
        else:
            rec_start_dt = parse_obs_start(recording.name) or parse_obs_start(str(recording))
            if rec_start_dt is not None:
                rec_start_source = "filename"

    if rec_start_dt is None:
        print("錯誤：無法判斷錄音開始時間，請用 --start \"YYYY-MM-DD HH:MM:SS\" 指定。", file=sys.stderr)
        return 2

    print(f"錄音開始時間：{rec_start_dt.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}（來源：{rec_start_source}）")

    rec_start = rec_start_dt.timestamp()
    duration = _ffprobe_duration(recording)

    db_path = Path(args.db) if args.db else REPO_ROOT / "marvin.db"
    speech_log_dir = Path(args.speech_log_dir) if args.speech_log_dir else REPO_ROOT
    bot_log_dir = Path(args.bot_log_dir) if args.bot_log_dir else REPO_ROOT

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

    speech_rows = read_speech_log(speech_log_dir)
    bot_log_lines = _read_log_rotations(bot_log_dir, "bot_main.log", 5)
    bot_acks = parse_bot_log_acks(bot_log_lines)

    events = collect_events(human_rows, speech_rows, bot_acks, rec_start=rec_start, duration=duration)

    out_txt = Path(args.output) if args.output else recording.with_name(recording.stem + ".timeline.txt")
    out_json = out_txt.with_suffix(".json")

    rec_start_str = rec_start_dt.strftime("%Y-%m-%d %H:%M:%S")

    out_txt.write_text(format_timeline(events, rec_start_str=rec_start_str, duration=duration), encoding="utf-8")
    out_json.write_text(
        json.dumps(
            {
                "recording": str(recording),
                "rec_start": rec_start_str,
                "rec_start_source": rec_start_source,
                "duration": duration,
                "events": events,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    counts: Dict[str, int] = {}
    for ev in events:
        counts[ev["kind"]] = counts.get(ev["kind"], 0) + 1

    print(f"共 {len(events)} 則事件，輸出到 {out_txt} 與 {out_json}")
    for kind, count in counts.items():
        print(f"  {kind}: {count}")

    return 0


def _do_review(args) -> int:
    candidates_path = Path(args.candidates)
    candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
    out_path = Path(args.output) if args.output else candidates_path.with_suffix(".md")
    out_path.write_text(render_review_md(candidates), encoding="utf-8")
    print(f"輸出到 {out_path}")
    return 0


def _do_validate(args) -> int:
    candidates_path = Path(args.candidates)
    candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
    errors = validate_candidates(candidates, REPO_ROOT)
    if errors:
        for e in errors:
            print(e)
        return 1
    print("OK")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="推廣素材：時間軸輸出 / 候選審閱稿 / 候選檔驗證")
    sub = parser.add_subparsers(dest="command", required=True)

    p_timeline = sub.add_parser("timeline")
    p_timeline.add_argument("recording")
    p_timeline.add_argument("--start")
    p_timeline.add_argument("--db")
    p_timeline.add_argument("--speech-log-dir")
    p_timeline.add_argument("--bot-log-dir")
    p_timeline.add_argument("--obs-log-dir")
    p_timeline.add_argument("-o", "--output")

    p_review = sub.add_parser("review")
    p_review.add_argument("candidates")
    p_review.add_argument("-o", "--output")

    p_validate = sub.add_parser("validate")
    p_validate.add_argument("candidates")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.command == "timeline":
        return _do_timeline(args)
    if args.command == "review":
        return _do_review(args)
    if args.command == "validate":
        return _do_validate(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())

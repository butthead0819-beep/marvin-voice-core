"""DJ 串場觀察紀錄 — append-only jsonl，供一週觀察用 (9/30 使用者定)。

每段串場寫一筆進 records/dj_narration.jsonl，記主題(mode/topic)、素材(ctx/
song_material)、LLM 原始產出、清雜訊結果、最終口白、口白來源、真實秒數。

寫檔失敗 (perm / disk full) 必須 silent return — DJ 生成不能因 log 壞掉。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("MarvinBot.DJ.NarrationLog")

_LOG_PATH: Path = Path("records") / "dj_narration.jsonl"
_SONG_PLAYS_PATH: Path = Path("records") / "song_plays.jsonl"
_SONG_SKIPS_PATH: Path = Path("records") / "song_skips.jsonl"


def _append_jsonl(path: Path, record: dict) -> None:
    entry = {"ts": time.time(), **record} if "ts" not in record else dict(record)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug(f"[DJ Narration Log] write failed ({path}): {e}")


def log_dj_narration(record: dict) -> None:
    """Append one jsonl entry（自動補 "ts": time.time()）。Silent on failure。"""
    _append_jsonl(_LOG_PATH, record)


def log_song_play(record: dict) -> None:
    """Append 一筆開播/口白歸屬紀錄到 records/song_plays.jsonl。Silent on failure。"""
    _append_jsonl(_SONG_PLAYS_PATH, record)


def log_song_skip(record: dict) -> None:
    """Append 一筆 skip 紀錄到 records/song_skips.jsonl。Silent on failure。"""
    _append_jsonl(_SONG_SKIPS_PATH, record)


_TAIL_BYTES = 2_000_000


def _song_key(song: str) -> str:
    """紀錄的 song 欄位（"歌手 - 歌名"）取 " - " 後半段；沒有就整段。"""
    part = song.split(" - ", 1)[1] if " - " in song else song
    return part


def recent_narrations_for_song(title: str, n: int = 2, path: Path | None = None) -> list[dict]:
    """這首歌最近 n 筆口白紀錄（舊→新）。比對方式：紀錄的 song 欄位取 " - " 後半段（沒有就整段），
    與 title 都過 music_recommender.normalize_title 後相等。讀檔失敗/壞行一律略過，回 []。"""
    from music_recommender import normalize_title

    target = normalize_title(title or "")
    if not target:
        return []
    log_path = path if path is not None else _LOG_PATH
    try:
        # 每段口白都查一次、同步跑在 event loop 上 → 只讀檔尾（約最近一千段），更早的不排除
        with log_path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - _TAIL_BYTES))
            lines = f.read().decode("utf-8", errors="ignore").splitlines()
        if size > _TAIL_BYTES:
            lines = lines[1:]  # 第一行可能被切半
    except Exception:
        return []
    hits: list[dict] = []
    for line in lines:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue
        song = rec.get("song") or ""
        if normalize_title(_song_key(str(song))) == target:
            hits.append(rec)
    return hits[-n:] if n > 0 else []


_LYRIC_QUOTE = re.compile(r"歌詞：『(.+)』")


def used_materials(records: list[dict]) -> dict[str, set[str]]:
    """彙整紀錄裡用過的素材：{"modes","song_materials","lyric_quotes","facets","jokes"}。"""
    used: dict[str, set[str]] = {
        "modes": set(), "song_materials": set(), "lyric_quotes": set(),
        "facets": set(), "jokes": set(),
    }
    for rec in records:
        if rec.get("mode"):
            used["modes"].add(rec["mode"])
        if rec.get("song_material"):
            used["song_materials"].add(rec["song_material"])
        lyric = rec.get("lyric_material") or ""
        m = _LYRIC_QUOTE.search(lyric)
        if m:
            used["lyric_quotes"].add(m.group(1))
        if rec.get("facet"):
            used["facets"].add(rec["facet"])
            if rec["facet"] == "lyric":
                fm = _LYRIC_QUOTE.search(rec.get("facet_text") or "")
                if fm:
                    used["lyric_quotes"].add(fm.group(1))
        if rec.get("source") == "joke" and rec.get("text"):
            used["jokes"].add(rec["text"])
    return used


async def probe_audio_seconds(path: str | None, timeout_s: float = 3.0) -> float | None:
    """ffprobe 量音檔真實秒數；path 空/不存在/ffprobe 失敗/逾時 → None。"""
    if not path or not os.path.exists(path):
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
            "-of", "csv=p=0", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout_s)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        return None
    try:
        return round(float(out), 2)
    except Exception:
        return None

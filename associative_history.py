"""Associative curation 30 天選曲紀錄（防重複選曲）。"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent
HISTORY_PATH = REPO_ROOT / "records" / "associative_picks.jsonl"
HISTORY_DAYS = 30
PROMPT_MAX_PICKS = 150


def load_recent_picks(now: float, path: Path | None = None, days: int = HISTORY_DAYS) -> list[str]:
    """讀最近 days 天內選過的歌，新的在前，同歌（normalize 後）只留最新一筆。"""
    from music_recommender import normalize_title

    target_path = path if path is not None else HISTORY_PATH
    try:
        lines = target_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    cutoff = now - days * 86400
    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            ts = float(data["ts"])
            artist = str(data["artist"])
            song = str(data["song"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if ts >= cutoff:
            entries.append((ts, artist, song))

    entries.sort(key=lambda e: e[0], reverse=True)

    seen_norm = set()
    result = []
    for ts, artist, song in entries:
        norm = normalize_title(song)
        if norm in seen_norm:
            continue
        seen_norm.add(norm)
        result.append(f"{artist} - {song}")
        if len(result) >= PROMPT_MAX_PICKS:
            break
    return result


def append_pick(artist: str, song: str, now: float, path: Path | None = None) -> None:
    """記錄一筆選曲。"""
    target_path = path if path is not None else HISTORY_PATH
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with target_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now, "artist": artist, "song": song}, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning(f"[AssociativeHistory] 寫入選曲紀錄失敗: {e}")


def is_repeat(song: str, recent_picks: list[str]) -> bool:
    """song 是否命中 recent_picks（"歌手 - 歌名" 清單，normalize 後比對）。"""
    from music_recommender import normalize_title

    target_norm = normalize_title(song)
    for pick in recent_picks:
        _, _, tail = pick.partition(" - ")
        pick_song = tail if tail else pick
        if normalize_title(pick_song) == target_norm:
            return True
    return False

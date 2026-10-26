"""真人點歌強化點歌者既有的歌手口味（docs/MARVIN_CORE_PRINCIPLES.md 原則 1，使用者 10/26 定）。

只強化「已存在、分數為正、非禁忌」且對得上這首歌歌手的 taste 項目，不新增項目——
YouTube 的 uploader/spotify_artist 雜訊（唱片公司、翻奏者）因此不會長成新喜好。
"""
from __future__ import annotations

from memory_check import matches_artist
from taste_fingerprint import artist_of

REINFORCE_DELTA = 0.5          # 每次點歌加的分（daily review 單日 ±1.5 的三分之一）
REINFORCE_GAP_S = 86400.0      # 同一項 24h 內只因點歌加一次（避免連點灌分）
REINFORCE_REASON = "song_request"
_UNKNOWN = {"未知", "未知藝術家"}


def artist_candidates(info: dict) -> list[str]:
    """依序收 title 抽出的藝人段、artist、spotify_artist、uploader，去空/未知/重複。"""
    raw = [
        artist_of(info.get("title", "")),
        info.get("artist"),
        info.get("spotify_artist"),
        info.get("uploader"),
    ]
    out: list[str] = []
    for v in raw:
        v = (v or "").strip() if isinstance(v, str) else ""
        if v and v not in _UNKNOWN and v not in out:
            out.append(v)
    return out


def keys_to_reinforce(taste: dict, taboos: set, candidates: list[str], now: float) -> list[str]:
    """taste 中符合全部條件的既有 key（保留 dict 順序）。"""
    if not candidates:
        return []
    cand_set = set(candidates)
    out: list[str] = []
    for key, entry in taste.items():
        if not isinstance(entry, dict):
            continue
        if float(entry.get("score", 0)) <= 0:
            continue
        if key in taboos:
            continue
        if not matches_artist(key, cand_set):
            continue
        if entry.get("last_reason") == REINFORCE_REASON and now - float(entry.get("last_update") or 0) < REINFORCE_GAP_S:
            continue
        out.append(key)
    return out

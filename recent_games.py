"""recent_games — 記住 Discord「正在玩」抓到過的遊戲。

2026-10-23：大家在 PS 上玩 Grounded 2，Discord 多半不顯示「正在玩」，10 月 95 次查詢
只有 13 次帶到遊戲脈絡。抓到時記下（誰、什麼遊戲、最後一次時間），當下抓不到就用
在場的人最近 RECENT_DAYS 天玩過的。零 LLM；讀寫失敗一律降級成空清單。

格式：{"games": {遊戲名: {成員: 最後看到的 ts}}}
"""
from __future__ import annotations

import json
import os
from pathlib import Path

STORE_PATH = Path("records/recent_games.json")
RECENT_DAYS = 7
DAY_S = 86400.0


def _load(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        games = data.get("games") if isinstance(data, dict) else None
        return games if isinstance(games, dict) else {}
    except Exception:
        return {}


def note_games(member: str, games: list[str], *, now: float, path: Path | None = None) -> None:
    """記下 member 正在玩 games（更新最後看到的時間）。永不 raise。"""
    if not member or not games:
        return
    p = Path(path or STORE_PATH)
    try:
        data = _load(p)
        for g in games:
            if g:
                data.setdefault(g, {})[member] = now
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps({"games": data}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        pass


def recent_games(members: list[str], *, now: float, days: int = RECENT_DAYS,
                 path: Path | None = None, limit: int = 3) -> list[str]:
    """members 中任何人在最近 days 天玩過的遊戲，最近的排前面，最多 limit 個。"""
    cutoff = now - days * DAY_S
    present = set(members)
    scored: list[tuple[float, str]] = []
    for game, seen in _load(Path(path or STORE_PATH)).items():
        if not isinstance(seen, dict):
            continue
        ts = max((t for m, t in seen.items() if m in present and isinstance(t, (int, float))), default=None)
        if ts is not None and ts >= cutoff:
            scored.append((ts, game))
    scored.sort(reverse=True)
    return [g for _, g in scored[:limit]]

"""game_aliases — 遊戲名誤聽對照表。

2026-10-23：語音辨識常把英文遊戲名聽成糊字（「圓地豆」「Gronde two」＝Grounded 2），
查詢模型因此答成別款遊戲（把「幫註」答成 Brotato）。這份對照表只收使用者確認過的
誤聽說法（不是 LLM 憑記憶生成），原句命中時在查詢 prompt 最前面加一段強提示。
見 docs/designs/game-alias-table.md。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

DATA_PATH = Path("data/game_aliases.json")


def _norm(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", s.casefold())


def load_aliases(path: Path | None = None) -> list[dict]:
    """讀 JSON 回傳 games 清單；檔案不存在/JSON 壞掉/格式不對一律回傳 []，永不 raise。"""
    try:
        data = json.loads(Path(path or DATA_PATH).read_text(encoding="utf-8"))
        games = data.get("games") if isinstance(data, dict) else None
        return games if isinstance(games, list) else []
    except Exception:
        return []


def match_games(text: str, entries: list[dict]) -> list[tuple[str, dict]]:
    """原句 → 命中的 (候選原字串, entry) 清單；不比對 name 本身。"""
    t = _norm(text)
    if not t:
        return []

    out: list[tuple[str, dict]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        heard = entry.get("heard")
        aka = entry.get("aka")
        zh = entry.get("zh")
        if not isinstance(heard, list) or not isinstance(aka, list):
            continue
        candidates: list[str] = list(heard) + list(aka)
        if isinstance(zh, str) and zh:
            candidates.append(zh)
        candidates.sort(key=lambda c: len(_norm(c)), reverse=True)

        for cand in candidates:
            norm_cand = _norm(cand)
            if norm_cand and norm_cand in t:
                out.append((cand, entry))
                break

    return out


def alias_hint(matches: list[tuple[str, dict]]) -> str:
    """命中清單 → 強提示字串；空清單回 ""。"""
    if not matches:
        return ""

    sentences = []
    for heard, entry in matches:
        name = entry.get("name", "")
        zh = entry.get("zh")
        if zh:
            sentences.append(f"「{heard}」＝遊戲《{name}》（{zh}）")
        else:
            sentences.append(f"「{heard}」＝遊戲《{name}》")

    return "【語音辨識常把遊戲名聽糊：" + "；".join(sentences) + "。回答時以這款遊戲為準，不要猜成別款遊戲】"

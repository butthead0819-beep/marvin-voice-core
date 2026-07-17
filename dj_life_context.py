"""DJ 串場的「最近生活素材」抽取（純函式，無 I/O）。

DJ 只串歌名唸起來像播報清單；摻進最近幾天的生活核心句、用雞湯口吻敘事才像真人 DJ。
素材來源＝日記主題摘要的核心句（records/chat_summary_log.txt → DiaryEntry），
與主題歌單 (themed_playlist.gather_theme_brief) 同一批素材，但窗是「天」不是「小時」。

低顯著度的核心句（『無意義對話』那種）不當素材——熬出來的雞湯是水。
"""
from __future__ import annotations

import datetime as _dt

DEFAULT_DAYS = 3.0
DEFAULT_MAX_CORES = 3      # 每則 DJ 只需要幾顆料；餵多了是白燒 token
DEFAULT_MAX_LEN = 40       # 單句上限，長摘要截斷


def _fields(entry) -> tuple[str | None, str | None, str]:
    """相容 tuple (ts_str, core[, salience]) 與 DiaryEntry 物件。"""
    if isinstance(entry, tuple):
        return (entry[0], entry[1], entry[2] if len(entry) > 2 else "中")
    return (getattr(entry, "ts_str", None), getattr(entry, "core", None),
            getattr(entry, "salience", "中"))


def recent_life_cores(summary_entries, *, now: float, days: float = DEFAULT_DAYS,
                      max_cores: int = DEFAULT_MAX_CORES,
                      max_len: int = DEFAULT_MAX_LEN) -> list[str]:
    """近 days 天的生活核心句（舊→新），最多 max_cores 條。無素材回 []。

    高顯著度標【重點】——那是最獨特/難忘的事（某人的計畫、罕見的事），
    讓 DJ 優先繞它熬湯，別被反覆出現的通用閒聊帶偏。壞時戳直接跳過。
    """
    cutoff = now - days * 86400.0
    cores: list[str] = []
    for e in summary_entries:
        ts_str, core, salience = _fields(e)
        if not core or not str(core).strip():
            continue
        if str(salience).strip() == "低":
            continue
        try:
            ts = _dt.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").timestamp()
        except (ValueError, TypeError):
            continue
        if ts < cutoff:
            continue
        c = str(core).strip()[:max_len]
        cores.append(f"【重點】{c}" if str(salience).strip() == "高" else c)
    return cores[-max_cores:]

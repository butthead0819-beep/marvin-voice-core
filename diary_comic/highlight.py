"""精華處理器：從逐字稿找「爆笑時刻」+ 前情笑點。

洞察（Jack 2026-06-21）：一群人同時哈哈笑，前幾句一定是精華。
STT 常把哄堂笑（重疊語音）收成一筆超長哈哈哈 → 實務訊號 = 一筆爆笑（≥N 哈 / 笑死）。

純函式、不碰 DB：吃 (speaker, text, ts) 的時序 list，回 Highlight。
未來可餵進漫畫當 beats（比中性摘要更能抓真精華）。
"""
from __future__ import annotations

import datetime as _dt
import math
import re
from dataclasses import dataclass, field

_LAUGH = re.compile(r"哈哈|笑死|笑翻|太好笑|笑爛|ㄏㄏ|噴飯")
_STRONG_WORDS = ("笑死", "笑翻", "太好笑", "笑爛", "噴飯")

LAUGH_RATIO = 0.5            # 發聲人數 / 在場人數 ≥ 此比例才算哄堂
LAUGH_MATCH_WINDOW_S = 2.0   # laugh_event 對 transcript 笑筆的時間對齊容差


def is_laugh(text: str) -> bool:
    return bool(_LAUGH.search(text or ""))


def enough_laughter(vocalizers: int, present: int, ratio: float = LAUGH_RATIO) -> bool:
    """哄堂判定：發聲人數是否達在場人數的比例門檻。

    在場越多、要越多人一起笑才算真精華；親密小房一人笑（聽眾笑講者的梗）就算。
    present<=0（不知道在場）→ 不擋，資料缺失不懲罰。
    """
    if present <= 0:
        return True
    return vocalizers >= max(1, math.ceil(present * ratio))


def count_concurrent_voices(last_spoken_map: dict, now: float, window: float = 3.0) -> int:
    """時間窗內有幾個不同 user 剛發過聲（笑聲當下的同時開麥人數）。

    last_spoken_map: user_id -> 最後人聲時間戳（sink.user_last_spoken_time）。
    """
    return sum(1 for ts in last_spoken_map.values()
               if ts and 0 <= now - ts <= window)


def laugh_strength(text: str) -> int:
    """爆笑強度：哈的數量 + 關鍵詞加成（笑死/太好笑…+5）。"""
    text = text or ""
    s = text.count("哈")
    if any(w in text for w in _STRONG_WORDS):
        s += 5
    return s


@dataclass
class Highlight:
    ts: float
    laugher: str                 # 觸發爆笑那筆的說話者
    laugh_text: str
    strength: int
    setup: list[tuple[str, str]] = field(default_factory=list)  # 前情 (說話者, 內容句)


def _room_gate(sp, ts, laugh_events) -> bool:
    """笑筆對齊 laugh_event（同 speaker + 時間相近）→ 套哄堂比例閘。

    無 laugh_events 或對不到（歷史資料無快照）→ True，不擋。
    """
    if not laugh_events:
        return True
    best = None
    for ev in laugh_events:
        if ev.get("speaker") != sp:
            continue
        d = abs(float(ev.get("timestamp", 0.0)) - ts)
        if d <= LAUGH_MATCH_WINDOW_S and (best is None or d < best[0]):
            best = (d, ev)
    if best is None:
        return True  # 對不到快照 → 不懲罰
    ev = best[1]
    return enough_laughter(int(ev.get("vocalizers", 0)), int(ev.get("present", 0)))


def find_highlights(rows, *, min_strength: int = 5, merge_window_s: float = 30,
                    setup_lines: int = 3, lookback_s: float = 120,
                    laugh_events=None) -> list[Highlight]:
    """找爆笑時刻：強度 ≥ min_strength 的笑，往前抓 setup_lines 句非笑內容當笑點。

    rows: (speaker, text, ts) 時序 list（依 ts 升冪）。相鄰爆笑在 merge_window_s 內視為同一哄堂。
    laugh_events: 笑聲當下的同時發聲快照 [{speaker,timestamp,vocalizers,present}]；
                  有給就套「多人笑/在場人數」比例閘濾掉陪笑（無給→行為不變）。
    """
    hits = [i for i, (_s, t, _ts) in enumerate(rows) if laugh_strength(t) >= min_strength]
    moments: list[int] = []
    last_ts: float | None = None
    for i in hits:
        ts = rows[i][2]
        if last_ts is None or ts - last_ts > merge_window_s:
            moments.append(i)
        last_ts = ts

    out: list[Highlight] = []
    for idx in moments:
        sp, txt, ts = rows[idx]
        if not _room_gate(sp, ts, laugh_events):
            continue  # 不夠多人笑（相對在場）→ 視為陪笑，跳過
        setup: list[tuple[str, str]] = []
        k = idx - 1
        while k >= 0 and len(setup) < setup_lines and ts - rows[k][2] <= lookback_s:
            s2, t2, _ = rows[k]
            if not is_laugh(t2) and len((t2 or "").strip()) > 3:
                setup.append((s2, t2))
            k -= 1
        setup.reverse()
        out.append(Highlight(ts=ts, laugher=sp, laugh_text=txt,
                             strength=laugh_strength(txt), setup=setup))
    return out


_CLEAN_SYS = (
    "你是對話精華整理員。下面是一段語音逐字稿片段（STT 有雜訊、可能糊），最後大家爆笑。"
    "請還原這段為什麼好笑，用一句清楚好讀的話描述笑點（繁中、≤30 字、保留梗）。只回那句話。"
)


def _setup_text(h: "Highlight") -> str:
    return "；".join(t for _s, t in h.setup)


def build_clean_prompt(h: "Highlight") -> tuple[str, str]:
    user = f"逐字稿：\n{_setup_text(h)}\n（接著大家：{h.laugh_text}）\n\n一句話講笑點："
    return _CLEAN_SYS, user


def clean_highlight(h: "Highlight", generate_fn=None) -> str:
    """把糊掉的 STT 笑點用 LLM 還原成一句清楚的話。無 LLM/失敗 → 原始拼接 fallback。"""
    fallback = _setup_text(h)[:40] or "（笑點）"
    if generate_fn is None:
        return fallback
    try:
        return (generate_fn(*build_clean_prompt(h)) or "").strip() or fallback
    except Exception:
        return fallback


def contrast_score(h: "Highlight") -> int:
    """反差分：笑點強度 + setup 內部跳躍（第一句↔最後一句越不像 = 越意外）。"""
    score = max(h.strength, laugh_strength(h.laugh_text))
    if len(h.setup) >= 2:
        sim = re_sim(h.setup[0][1], h.setup[-1][1])
        score += int((1.0 - sim) * 4)
    return score


def meme_needs_marvin(h: "Highlight", strong_threshold: int = 10) -> bool:
    """強反差 → 梗自己講（不要 Marvin，避免解釋笑話）；反差中 → Marvin 補刀。"""
    return contrast_score(h) < strong_threshold


def re_sim(a: str, b: str) -> float:
    import difflib
    return difflib.SequenceMatcher(None, a or "", b or "").ratio()


def highlight_to_entry(h: "Highlight", core: str | None = None):
    """精華 → 漫畫能吃的 DiaryEntry：core=笑點、speakers=參與者、aside=笑聲。"""
    from diary_comic.parser import DiaryEntry
    ts_str = _dt.datetime.fromtimestamp(h.ts).strftime("%Y-%m-%d %H:%M:%S")
    speakers = list(dict.fromkeys([s for s, _ in h.setup] + [h.laugher]))
    return DiaryEntry(ts_str=ts_str, core=(core or _setup_text(h)[:40] or "（笑點）"),
                      speakers=speakers, aside=h.laugh_text)

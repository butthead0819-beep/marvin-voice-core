"""DJ 串場的「最近生活素材」抽取（純函式，無 I/O）。

DJ 只串歌名唸起來像播報清單；摻進最近幾天的生活核心句、用雞湯口吻敘事才像真人 DJ。
素材來源＝日記主題摘要的核心句（records/chat_summary_log.txt → DiaryEntry），
與主題歌單 (themed_playlist.gather_theme_brief) 同一批素材，但窗是「天」不是「小時」。

低顯著度的核心句（『無意義對話』那種）不當素材——熬出來的雞湯是水。

Privacy：帶 participants 的 entry，participants 必須是 present_speakers 子集才帶入；
is_sensitive=True 且 present_speakers 未知時（None）一律丟掉（保守原則）。
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

DEFAULT_DAYS = 3.0
DEFAULT_MAX_CORES = 3      # 每則 DJ 只需要幾顆料；餵多了是白燒 token
DEFAULT_MAX_LEN = 40       # 單句上限，長摘要截斷

_SENTINEL = object()       # 區分「呼叫端未傳 present_speakers」與「明確傳 None」


@dataclass(frozen=True)
class LifeCore:
    """生活素材：文字 + 語義 tag + 事件主角。

    speakers 給上層（dj_topic_selector.select_mode）判斷「這件事的主角現在在不在場」，
    跟 privacy filter（是否安全提及）是兩件事——後者在本模組內已處理，speakers 只是
    原樣帶出去讓呼叫端自己決定要不要用。
    """
    text: str
    meme_id: str = ""
    speakers: tuple[str, ...] = ()



def _fields(entry) -> tuple[str | None, str | None, str]:
    """相容 tuple (ts_str, core[, salience]) 與 DiaryEntry 物件。"""
    if isinstance(entry, tuple):
        return (entry[0], entry[1], entry[2] if len(entry) > 2 else "中")
    return (getattr(entry, "ts_str", None), getattr(entry, "core", None),
            getattr(entry, "salience", "中"))


def _is_privacy_safe(
    entry,
    present_speakers: set[str] | None,
) -> bool:
    """privacy filter：回 True = 可帶入 context，False = 丟掉。

    規則（優先順序）：
    1. is_sensitive=True 且 present_speakers=None → False（不知道誰在場，保守丟掉）
    2. 決定 participants：
       - entry.participants 有值 → 直接用
       - is_sensitive=True + participants=None → 退用 entry.speakers（DiaryEntry 的對話人）
       - 其餘（非敏感、無 participants）→ None（公開事件）
    3. participants 非 None → 需為 present_speakers 子集；present_speakers=None 視為全員在場通過。
    4. 其餘 → True
    """
    is_sensitive = bool(getattr(entry, "is_sensitive", False))

    # 規則1：敏感事件且在場人不明 → 丟
    if is_sensitive and present_speakers is None:
        return False

    # 決定 participants（規則2）
    participants = getattr(entry, "participants", None)
    if participants is None and is_sensitive:
        # DiaryEntry.speakers 作為 sensitive entry 的參與者兜底
        _speakers = getattr(entry, "speakers", None)
        if _speakers:
            participants = _speakers

    # 規則3：有 participants → 子集判斷
    if participants is not None:
        if present_speakers is None:
            return True  # 未指定在場人 = 全員在場，通過
        return set(participants).issubset(present_speakers)

    return True



def _build_life_cores(
    summary_entries,
    *,
    now: float,
    days: float,
    max_cores: int,
    max_len: int,
    present_speakers,
) -> list[LifeCore]:
    do_filter = present_speakers is not _SENTINEL
    effective_speakers = present_speakers if do_filter else None

    cutoff = now - days * 86400.0
    cores: list[LifeCore] = []
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
        # Privacy filter（只在 do_filter=True 時觸發）
        if do_filter and not _is_privacy_safe(e, effective_speakers):
            continue
        c = str(core).strip()[:max_len]
        label = f"【重點】{c}" if str(salience).strip() == "高" else c
        raw_meme = getattr(e, "meme_id", "")
        # 只接受真正的 str，防止 MagicMock 等非字串被誤判為有效 meme_id/speakers
        meme_id = raw_meme.strip() if isinstance(raw_meme, str) else ""
        raw_speakers = getattr(e, "speakers", None)
        speakers = tuple(raw_speakers) if isinstance(raw_speakers, (list, tuple)) else ()
        cores.append(LifeCore(label, meme_id, speakers))

    return cores[-max_cores:]


def recent_life_cores(
    summary_entries,
    *,
    now: float,
    days: float = DEFAULT_DAYS,
    max_cores: int = DEFAULT_MAX_CORES,
    max_len: int = DEFAULT_MAX_LEN,
    present_speakers: set[str] | None = _SENTINEL,
) -> list[str]:
    """近 days 天的生活核心句（舊→新），最多 max_cores 條。無素材回 []。

    high salience 標【重點】——那是最獨特/難忘的事（某人的計畫、罕見的事），
    讓 DJ 優先繞它熬湯，別被反覆出現的通用閒聊帶偏。壞時戳直接跳過。

    present_speakers：
      - 不傳（Sentinel）或傳 None → 不做 privacy filter（向後相容）
      - 傳入 set[str]  → 啟用 participant-aware filter：
          * is_sensitive=True 且 present_speakers=None：已在呼叫端傳入 None，
            本函式的 Sentinel 不觸發此規則；但若呼叫端明確傳 None，
            _is_privacy_safe 會視為「在場人未知」→ 丟掉敏感事件。
          * entry.participants 不是 None → 需為 present_speakers 子集。

    回傳純字串/`(text, meme_id)`——不帶 speakers。需要主角資訊（判斷是否在場）
    的呼叫端請改用 recent_life_cores_with_speakers。
    """
    cores = _build_life_cores(
        summary_entries, now=now, days=days, max_cores=max_cores,
        max_len=max_len, present_speakers=present_speakers,
    )
    return [(c.text, c.meme_id) if c.meme_id else c.text for c in cores]


def recent_life_cores_with_speakers(
    summary_entries,
    *,
    now: float,
    days: float = DEFAULT_DAYS,
    max_cores: int = DEFAULT_MAX_CORES,
    max_len: int = DEFAULT_MAX_LEN,
    present_speakers: set[str] | None = _SENTINEL,
) -> list[LifeCore]:
    """同 recent_life_cores，但保留每條素材的事件主角（entry.speakers）。

    給 dj_topic_selector.select_mode 用：主角現在不在頻道裡的生活素材，
    上層可以直接跳過換下一個候選，不用讓 LLM 去猜「這件事是不是點播者的」。
    """
    return _build_life_cores(
        summary_entries, now=now, days=days, max_cores=max_cores,
        max_len=max_len, present_speakers=present_speakers,
    )

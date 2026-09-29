"""聽覺放大鏡導聆稿抓取（docs/PLAN_audiophile_music_tour.md Phase 2）。

重用 grounded_answer（free→付費鏈 + PaidUsageGuard 記帳 + L1/L2 幻覺 guard），不自開 client、
不寫死 model。快取在 SongKnowledgeStore 同檔但獨立 key「audiophile::…」——不跟
get_or_extract_insight 的記錄共用 dict（那邊 set 是整份覆寫，共用會互洗欄位）。
失敗回保底台詞且不寫快取。
"""
from __future__ import annotations

import logging
import re
import time

from pypinyin import lazy_pinyin

from dj_prompt_builder import build_audiophile_guide_prompt, build_album_tracklist_prompt
from intent_agents.grounded_qa_agent import grounded_answer

logger = logging.getLogger(__name__)

# 背景預渲染用，grounded 搜尋+寫 100 字比 AmbientQA 的 8s 慢
GUIDE_TIMEOUT_S = 20.0
FALLBACK_GUIDE_TEMPLATE = "這首〈{title}〉我就不多嘴了，戴好耳機，從第一個音開始聽。"
_KEY_PREFIX = "audiophile::"

MAX_TOUR_TRACKS = 20
TRACKLIST_MAX_CHARS = 800   # AmbientQA 預設 140 字截斷會把長專輯砍半
_ALBUM_KEY_PREFIX = "album_tracklist::"
_TRACK_LINE_RE = re.compile(r"^\s*\d{1,2}\s*[.、)）]\s*(.+?)\s*$")


def _song_label(title: str, artist: str) -> str:
    return f"{artist} - {title}" if artist else title


async def fetch_audiophile_guide(
    title: str,
    artist: str,
    *,
    free_client,
    paid_client,
    guard,
    store,
) -> str:
    """title/artist → 導聆台詞；快取命中零 API 呼叫，失敗回保底台詞且不寫快取。"""
    label = _song_label(title, artist)
    key = _KEY_PREFIX + label

    cached = (store.get(key) or {}).get("audiophile_guide")
    if cached:
        return cached

    res = None
    try:
        res = await grounded_answer(
            free_client, paid_client, guard, label,
            system_prompt=build_audiophile_guide_prompt(label),
            caller="audiophile_guide",
            timeout=GUIDE_TIMEOUT_S,
        )
    except Exception as e:
        logger.warning(f"[Audiophile] grounded_answer 例外: {e}")

    if res is None:
        logger.info(f"[Audiophile] {label} 查不到可靠資料，回保底台詞")
        return FALLBACK_GUIDE_TEMPLATE.format(title=title)

    text, sources = res
    store.set(key, {"audiophile_guide": text, "sources": sources, "ts": time.time()})
    return text


async def render_audiophile_guide(
    info: dict,
    *,
    title: str,
    artist: str,
    free_client,
    paid_client,
    guard,
    store,
    tts_engine,
    probe_duration,
) -> None:
    """抓導聆稿 + TTS 預渲染 + 量真實秒數，就地標記 info（Phase 3 播放端讀這四個欄位）。

    TTS 失敗或量不到秒數 → 仍標記為導聆歌但沒音檔（Phase 3 跳過 pre-roll、照樣從 0 播），
    不拋例外。
    """
    text = await fetch_audiophile_guide(
        title, artist, free_client=free_client, paid_client=paid_client,
        guard=guard, store=store,
    )

    audio = None
    dur = 0.0
    try:
        audio = await tts_engine.generate_audio(text)
    except Exception as e:
        logger.warning(f"[Audiophile] 導聆 TTS 渲染失敗: {e}")

    if audio:
        dur = await probe_duration(audio)

    if not audio or dur <= 0:
        audio, dur = None, 0.0

    info['_audiophile_guide'] = True
    info['_audiophile_guide_text'] = text
    info['_audiophile_guide_audio'] = audio
    info['_audiophile_guide_dur'] = dur


_NON_ALNUM_RE = re.compile(r"[^0-9a-z]")


def _pinyin_key(s: str) -> str:
    """轉拼音 + 小寫 + 只留英數：簡繁同音（双截棍/雙截棍）、空白、標點、【】[]｜ 全部抹平。"""
    return _NON_ALNUM_RE.sub("", "".join(lazy_pinyin(s or "")).lower())


def resolved_matches_track(info: dict, track: str) -> bool:
    """YouTube 解析結果是不是這首歌——曲名（拼音正規化）必須出現在影片標題或
    track metadata 裡。配到別首歌（例「告五人 過場」→〈在這座城市遺失了你〉）回 False，
    呼叫端跳過，不播錯歌（說錯不如沒說）。同音字會誤判為 True，這層是盡力而為的守門。"""
    want = _pinyin_key(track)
    if not want:
        return False
    hay = _pinyin_key(f"{info.get('title') or ''} {info.get('track') or ''}")
    return want in hay


def parse_tracklist(text: str) -> list[str]:
    """「1. 歌名」編號行 → 歌名清單（剝《》「」引號、去重、最多 MAX_TOUR_TRACKS）；非編號行忽略。"""
    out: list[str] = []
    for line in (text or "").splitlines():
        m = _TRACK_LINE_RE.match(line)
        if not m:
            continue
        name = m.group(1).strip().strip("《》「」\"'").strip()
        if name and name not in out:
            out.append(name)
        if len(out) >= MAX_TOUR_TRACKS:
            break
    return out


async def fetch_album_tracklist(
    artist: str, album: str, *, free_client, paid_client, guard, store,
) -> list[str]:
    """artist/album → 官方曲目清單（/tour 用）；快取命中零 API 呼叫。
    快取 key「album_tracklist::<artist> - <album>」；查不到 / 幻覺 guard 擋下 /
    解析不出編號行 → []，不寫快取。"""
    key = f"{_ALBUM_KEY_PREFIX}{artist} - {album}"
    cached = (store.get(key) or {}).get("tracks")
    if cached:
        return list(cached)
    res = None
    try:
        res = await grounded_answer(
            free_client, paid_client, guard, f"{artist}《{album}》",
            system_prompt=build_album_tracklist_prompt(artist, album),
            caller="album_tour_tracklist",
            timeout=GUIDE_TIMEOUT_S,
            max_chars=TRACKLIST_MAX_CHARS,
        )
    except Exception as e:
        logger.warning(f"[Audiophile] 曲目查證例外: {e}")
    if res is None:
        logger.info(f"[Audiophile] {artist}《{album}》查不到可靠曲目")
        return []
    text, sources = res
    tracks = parse_tracklist(text)
    if not tracks:
        logger.info(f"[Audiophile] {artist}《{album}》回應解析不出曲目：{text[:60]!r}")
        return []
    store.set(key, {"tracks": tracks, "sources": sources, "ts": time.time()})
    return tracks

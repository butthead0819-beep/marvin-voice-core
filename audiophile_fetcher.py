"""聽覺放大鏡導聆稿抓取 + iTunes 正規化（2026-09-29 重新設計）。

重用 grounded_answer（free→付費鏈 + PaidUsageGuard 記帳 + L1/L2 幻覺 guard），不自開 client、
不寫死 model。快取在 SongKnowledgeStore 同檔但獨立 key「audiophile::…」——不跟
get_or_extract_insight 的記錄共用 dict（那邊 set 是整份覆寫，共用會互洗欄位）。

正規化（歌手/歌名/專輯/年份）優先走 Shazam 音訊認歌（shazam_identify，從串流切一段送
Shazam 拿乾淨歌名，再用它查 iTunes 換繁體+年份）——比直接拿 YouTube 髒標題查 iTunes
命中率高很多；Shazam 是非官方 API 隨時可能失效，任何失敗（含守門不通過）都退回原本的
iTunes 髒標題查詢路徑。寫進獨立 key「canon::<video_id>」——跟導聆稿分開查證，各自
失敗互不影響。

DJ 串場自動觸發（song_guide_for_dj）受免費層每日預算節流（AutoGuideBudget）：免費
gemini-2.5-flash 一天只有 20 次、跟 AmbientQA 共用，autopilot 背景串場不能任由每次都燒；
真人點歌（/guide_song、/tour）不受此預算，可以走免費→付費鏈。
"""
from __future__ import annotations

import asyncio
import functools
import logging
import re
import time

from pypinyin import lazy_pinyin

from dj_prompt_builder import build_album_tracklist_prompt, build_audiophile_guide_prompt
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


async def _fetch_guide(label: str, *, free_client, paid_client, guard, store) -> str | None:
    """label（歌手 - 歌名）→ 導聆稿，或 None（查不到/幻覺 guard 擋下）。
    快取命中零 API 呼叫；成功寫快取，失敗/None 不寫快取。"""
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
        logger.info(f"[Audiophile] {label} 查不到可靠資料")
        return None

    text, sources = res
    store.set(key, {"audiophile_guide": text, "sources": sources, "ts": time.time()})
    return text


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
    text = await _fetch_guide(label, free_client=free_client, paid_client=paid_client,
                              guard=guard, store=store)
    if text is None:
        logger.info(f"[Audiophile] {label} 回保底台詞")
        return FALLBACK_GUIDE_TEMPLATE.format(title=title)
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


# ── 正規化：iTunes Search（免費、結構化、零幻覺）─────────────────────────────
_CANON_KEY_PREFIX = "canon::"
_SINGLE_SUFFIX_RE = re.compile(r"\s*-\s*(Single|EP)\s*$", re.I)

_NON_ALNUM_RE = re.compile(r"[^0-9a-z]")
_ARTIST_SPLIT_RE = re.compile(r"\s*(?:&|,|、|/|\bx\b|\bfeat\.?)\s*", re.I)
_PAREN_RE = re.compile(r"\s*[\(（][^)）]*[\)）]")


def _pinyin_key(s: str) -> str:
    """轉拼音 + 小寫 + 只留英數：簡繁同音（双截棍/雙截棍）、空白、標點、【】[]｜ 全部抹平。"""
    return _NON_ALNUM_RE.sub("", "".join(lazy_pinyin(s or "")).lower())


def _artist_matches(meta_artist: str, hay: str) -> bool:
    """iTunes 歌手（合唱拆開任一位）的拼音 key 出現在 hay（原始標題/頻道名/乾淨歌手）裡才算對得上。
    羅馬拼音↔中文名（A-Sun↔阿桑）會誤擋——寧可少正規化，不要配錯歌手。"""
    h = _pinyin_key(hay)
    return any(k and k in h for k in (_pinyin_key(p) for p in _ARTIST_SPLIT_RE.split(meta_artist or "")))


async def resolve_canon(store, video_id: str, clean_title: str, clean_artist: str, *,
                        artist_hay: str = "", stream_url: str = "", duration=None,
                        breaker=None, identify=None, fetch=None) -> dict | None:
    """video_id → 正規化 {artist, title, album, year, source, ts}，或 None（沒有
    video_id / 查不到 / 曲名配不上）。快取命中零查詢。

    查證順序：
      1. Shazam 音訊認歌（有 stream_url + breaker 才會嘗試）：認到的曲名要在原始
         標題裡守門通過，才拿它的乾淨歌名去查 iTunes 換繁體+年份；iTunes 也確認得
         上就用 iTunes 版本（source="shazam+itunes"），確認不上就退回用 Shazam 自己
         給的資料（source="shazam"，年份未知）。
      2. Shazam 沒有/失敗/守門不過 → 退回原本拿 YouTube 髒標題查 iTunes 的路徑
         （source="itunes"，行為與改版前完全相同）。

    曲名守門：回應的曲名跟查詢曲名（拼音正規化後）互不包含就不採用——避免跨語言
    救援自信地配錯歌。
    歌手守門：髒標題夾歌詞時曲名可能「互相包含」誤放行（9/29 真機：〈我的秘密〉歌詞
    「…靠近」配成吳莫愁〈靠近〉），所以歌手也要在 clean_artist + artist_hay 裡對得上。
    """
    if not video_id:
        return None

    cached = store.get(_CANON_KEY_PREFIX + video_id)
    if cached:
        return cached

    import itunes_cover
    import shazam_identify

    shz = None
    if stream_url and breaker is not None:
        try:
            shz = await (identify or shazam_identify.identify)(
                stream_url, duration=duration, breaker=breaker,
            )
        except Exception as e:
            logger.info(f"[Canon] Shazam 查證例外: {e}")
            shz = None

    if shz is not None:
        core = _PAREN_RE.sub("", shz["title"]).strip()
        if not core or _pinyin_key(core) not in _pinyin_key(f"{clean_title} {artist_hay}"):
            logger.info(f"[Canon] Shazam 認到《{shz['title']}》不在原標題，不採用")
            shz = None

    if shz is not None:
        meta = None
        try:
            meta = await itunes_cover.resolve_metadata(
                core, shz["artist"],
                fetch=fetch or functools.partial(itunes_cover._default_fetch, country="TW"),
            )
        except Exception as e:
            logger.info(f"[Canon] Shazam 認到的歌查 iTunes 例外: {e}")
            meta = None

        if (meta and meta.get("title") and meta.get("artist")
                and _pinyin_key(core) in _pinyin_key(meta["title"])
                and _artist_matches(meta["artist"], shz["artist"])):
            album = (meta.get("album") or "").strip()
            if album and _SINGLE_SUFFIX_RE.search(album):
                album = None
            canon = {
                "artist": meta["artist"], "title": meta["title"], "album": album or None,
                "year": meta.get("year"), "source": "shazam+itunes", "ts": time.time(),
            }
        else:
            album = (shz.get("album") or "").strip()
            if album and _SINGLE_SUFFIX_RE.search(album):
                album = None
            canon = {
                "artist": shz["artist"], "title": core, "album": album or None,
                "year": None, "source": "shazam", "ts": time.time(),
            }
        store.set(_CANON_KEY_PREFIX + video_id, canon)
        return canon

    try:
        meta = await itunes_cover.resolve_metadata(
            clean_title, clean_artist or None,
            fetch=fetch or functools.partial(itunes_cover._default_fetch, country="TW"),
        )
    except Exception as e:
        logger.info(f"[Canon] iTunes 查證例外: {e}")
        return None

    if not meta or not meta.get("title") or not meta.get("artist"):
        return None

    a, b = _pinyin_key(clean_title), _pinyin_key(meta["title"])
    if not a or not b or (a not in b and b not in a):
        logger.info(f"[Canon] iTunes 配到別首：查詢《{clean_title}》，回應《{meta['title']}》")
        return None

    if not _artist_matches(meta["artist"], f"{clean_artist} {artist_hay}"):
        logger.info(f"[Canon] iTunes 歌手對不上：查詢《{clean_title}》，回應 {meta['artist']}")
        return None

    album = (meta.get("album") or "").strip()
    if album and _SINGLE_SUFFIX_RE.search(album):
        album = None

    canon = {
        "artist": meta["artist"], "title": meta["title"], "album": album or None,
        "year": meta.get("year"), "source": "itunes", "ts": time.time(),
    }
    store.set(_CANON_KEY_PREFIX + video_id, canon)
    return canon


# ── DJ 串場自動觸發：免費層每日預算 ────────────────────────────────────────
# 免費 gemini-2.5-flash 一天只有 20 次、跟 AmbientQA 共用（只留 2 次給它，使用者 9/29 定：很少用），autopilot 背景串場不能
# 任由每次都打一次 grounded 查詢（安靜背景會把免費額度燒光）。全部 in-memory，
# 不落檔，bot 重啟歸零。真人點歌（human=True）不受此預算，見 song_guide_for_dj。
AUTO_MIN_INTERVAL_S = 90.0
AUTO_DAILY_CAP = 18
AUTO_FAIL_COOLDOWN_S = 7 * 86400

DJ_GUIDE_WAIT_S = 10.0


class AutoGuideBudget:
    """DJ 串場自動觸發（song_guide_for_dj, human=False）的免費層預算：全域最短間隔 +
    每日上限 + 單一 key 失敗冷卻。全部 in-memory（純節流，不是永久記錄，不用落檔）。

    `allow()` 回 True 的當下就登記 attempt 時間與當日計數，不等呼叫端事後補登記
    ——否則兩個呼叫在 allow 判斷完、record 之前之間插進來，會一起穿過間隔限制。
    """

    def __init__(self):
        self._last_attempt_ts: float = 0.0
        self._daily_count: int = 0
        self._daily_date: tuple | None = None
        self._fail_ts: dict[str, float] = {}

    def allow(self, key: str, now: float) -> bool:
        if now - self._last_attempt_ts < AUTO_MIN_INTERVAL_S:
            return False
        today = time.localtime(now)[:3]
        if self._daily_date != today:
            self._daily_date = today
            self._daily_count = 0
        if self._daily_count >= AUTO_DAILY_CAP:
            return False
        fail_ts = self._fail_ts.get(key)
        if fail_ts is not None and now - fail_ts < AUTO_FAIL_COOLDOWN_S:
            return False
        self._last_attempt_ts = now
        self._daily_count += 1
        return True

    def record(self, key: str, now: float, ok: bool) -> None:
        if ok:
            self._fail_ts.pop(key, None)
        else:
            self._fail_ts[key] = now


async def song_guide_for_dj(
    label: str,
    *,
    human: bool,
    free_client,
    paid_client,
    guard,
    store,
    budget: AutoGuideBudget,
    inflight: dict,
    wait_s: float = DJ_GUIDE_WAIT_S,
) -> str | None:
    """DJ 串場觸發的導聆稿：快取命中直接回、否則背景 task 查證，最多等 wait_s 秒
    （逾時回 None，但 task 會跑完把快取寫好，給下次用）。

    human=False（autopilot 自動觸發）：受 AutoGuideBudget 節流、只打免費層，不燒付費額度。
    human=True（真人點歌 /guide_song /tour）：不經 budget，走免費→付費鏈
    （grounded_answer 內建 guard 記帳）。

    同一 label 併發觸發共用同一個 in-flight task（inflight dict 由呼叫端跨呼叫持有），
    不會重複打 API。
    """
    if not label:
        return None

    cached = (store.get(_KEY_PREFIX + label) or {}).get("audiophile_guide")
    if cached:
        return cached

    task = inflight.get(label)
    if task is None:
        if not human:
            if not budget.allow(label, time.time()):
                return None

            async def _run():
                return await _fetch_guide(
                    label, free_client=free_client, paid_client=None, guard=None, store=store,
                )
        else:
            async def _run():
                return await _fetch_guide(
                    label, free_client=free_client, paid_client=paid_client, guard=guard, store=store,
                )

        task = asyncio.create_task(_run())
        inflight[label] = task

        def _on_done(t, lbl=label):
            inflight.pop(lbl, None)
            if not human:
                ok = False
                if not t.cancelled() and t.exception() is None:
                    ok = t.result() is not None
                budget.record(lbl, time.time(), ok=ok)

        task.add_done_callback(_on_done)

    try:
        return await asyncio.wait_for(asyncio.shield(task), wait_s)
    except Exception:
        return None


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

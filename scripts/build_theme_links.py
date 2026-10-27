"""scripts/build_theme_links.py — 主題連結 MVP 離線批次（docs/designs/theme-link-mvp.md A 段）。

手動或 launchd 每天跑一次。六步：
1. 歌卡（song_cards）：music_memory total_plays>=2 的歌，依播放數排隊，每晚最多 CARD_BATCH_LIMIT 張。
2. tag 收集（純函式，不打 LLM）：chat_summary_log.txt 近 14 天、meme_id 非空、在 ≥2 個不同
   日期出現的合格 tag。
3. heard_by（純函式）：voice_presence.jsonl join/leave 重建每個窗的在場者聯集。
4. tag 釋義＋敏感（theme_tags）：每個 tag 只打一次 LLM；last_seen 有更新才重算 heard_by。
5. 房間把關（theme_tags.room_veto）：該 tag 最近一個窗前 15 分鐘的 transcripts（<5 行不判斷）。
6. 配歌（theme_picks，粗篩，最終由 Associative 那次 LLM 呼叫決定）。

全部 LLM 呼叫走 bus：build_tiered_router(env={"GOOGLE_API_KEY": ...})——只放免費 key，
沒有付費兜底，不會掉到不熟華語歌的模型。呼叫之間 sleep 1.0s；單筆失敗只跳過該筆
（fail-open，不讓一首歌/一個 tag 的錯誤中斷整晚批次）。

用法：python scripts/build_theme_links.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 載 .env 拿 GOOGLE_API_KEY / GUILD_ID；launchd 跑時 env 不會自動帶（同 analyze_daily_feedback.py）
from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")

from theme_link import (  # noqa: E402
    CARD_BATCH_LIMIT,
    ROOM_VETO_LOOKBACK_MIN,
    ROOM_VETO_MIN_LINES,
    build_card_prompt,
    build_room_veto_prompt,
    build_theme_match_prompt,
    build_theme_meaning_prompt,
    collect_tag_occurrences,
    eligible_tags,
    parse_room_veto,
    parse_song_card,
    parse_theme_meaning,
    parse_theme_picks,
    reconstruct_present_users,
    select_card_queue,
    select_match_candidates,
    tpe_date_str,
)

logger = logging.getLogger("theme_link")

# 一律以 repo 根目錄為準，不依賴執行時的 cwd
DB_PATH = str(ROOT / "marvin.db")
MUSIC_MEMORY_PATH = str(ROOT / "music_memory.json")
CHAT_LOG_PATH = ROOT / "records" / "chat_summary_log.txt"
PRESENCE_LOG_PATH = ROOT / "data" / "voice_presence.jsonl"
SONG_KNOWLEDGE_PATH = ROOT / "records" / "song_knowledge.json"
LLM_SLEEP_S = 4.0  # 免費 Gemini RPM 低；1.0s 首次實跑一路撞限流
HEARD_BY_WINDOW_S = 600.0  # 窗前 10 分鐘（10 分鐘日誌）
# LLM 呼叫失敗（限流等回 None/空字串）＝「沒有結果」，不是「模型說不熟／敏感」：一律不寫入、下次重試。
# 連續失敗這麼多次就停掉該階段（額度多半已用完，繼續打只是空轉）。2026-10-27 首次實跑：
# 限流的 None 被存成 familiarity=低 / sensitive=1 / room_veto=1，且永不重算 → 露營等 tag 永久失效。
MAX_CONSECUTIVE_LLM_FAILURES = 3

# 批次走 bus 的 quick tier，並用 bus 既有的 env 覆寫把 gemini_free quick 換成 3.5-flash-lite（盲測歌卡用的就是它）。
# 為什麼不用 analyze（2026-10-27 實測）：gemini_free 的 analyze 是 gemini-3.5-flash，免費一天只有 20 次、
# bot 自己要用；gemini_free_25 的 analyze 是 3.5-flash-lite 但帶 reasoning_effort=none → 每次 400。
# quick tier 不帶 reasoning 參數。quick 預設 max_tokens=200 不夠一張卡，呼叫時給 BATCH_MAX_TOKENS。
BATCH_MODEL_OVERRIDES = {"GEMINI_FREE_QUICK_MODEL": "gemini-3.5-flash-lite"}
BATCH_MAX_TOKENS = 800


class LLMCallFailed(Exception):
    """router 回 None/空字串（限流、全 provider 失敗）。"""


async def _analyze_or_raise(router, user_p: str, *, caller: str, system: str) -> str:
    raw = await router.quick(user_p, caller=caller, system=system, json=True,
                             max_tokens=BATCH_MAX_TOKENS, temperature=0.3)
    if not raw or not str(raw).strip():
        raise LLMCallFailed(caller)
    return raw


def init_tables(con: sqlite3.Connection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS song_cards (
            video_id TEXT PRIMARY KEY, artist TEXT, title TEXT, imagery TEXT, deep_story TEXT,
            relation TEXT, weight TEXT, familiarity TEXT, created_at REAL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS theme_tags (
            tag TEXT PRIMARY KEY, meaning TEXT, sensitive INTEGER, songable INTEGER, room_veto INTEGER,
            heard_by TEXT, first_seen REAL, last_seen REAL, day_count INTEGER, updated_at REAL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS theme_picks (
            tag TEXT, video_id TEXT, rank INTEGER, layer TEXT, PRIMARY KEY (tag, video_id)
        )
    """)
    # 舊表（在加 songable 欄之前建的）補欄位
    cols = {r[1] for r in con.execute("PRAGMA table_info(theme_tags)")}
    if "songable" not in cols:
        con.execute("ALTER TABLE theme_tags ADD COLUMN songable INTEGER")
    con.commit()


def load_presence_events(path: Path | None = None) -> list[dict]:
    if path is None:
        path = PRESENCE_LOG_PATH
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def load_canon_names(path: Path) -> dict[str, tuple[str, str]]:
    """讀 song_knowledge.json 的 canon:: 歌名歌手正規化對照表（video_id -> (artist, title)）。
    key 不是 canon:: 開頭、或 artist/title 有一邊空的都不收。檔案不存在或壞掉 → {}。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, tuple[str, str]] = {}
    prefix = "canon::"
    for key, value in data.items():
        if not isinstance(key, str) or not key.startswith(prefix) or not isinstance(value, dict):
            continue
        artist = str(value.get("artist", "") or "").strip()
        title = str(value.get("title", "") or "").strip()
        if not artist or not title:
            continue
        out[key[len(prefix):]] = (artist, title)
    return out


def compute_heard_by(occurrence_timestamps: list[float], events: list[dict]) -> list[str]:
    """每個帶 tag 的窗（窗前 10 分鐘～時間戳）在場者聯集；某窗重建不出任何人就不算數
    （對空集合取聯集本就無影響，不需特殊處理）。"""
    heard: set[str] = set()
    for ts in occurrence_timestamps:
        heard |= reconstruct_present_users(events, window_start=ts - HEARD_BY_WINDOW_S, window_end=ts)
    return sorted(heard)


# ── 1. 歌卡 ──────────────────────────────────────────────────────────────

async def run_card_phase(con: sqlite3.Connection, router, songs: dict, *,
                         sleep_fn=asyncio.sleep, now: float | None = None,
                         canon_names: dict[str, tuple[str, str]] | None = None) -> int:
    now = now if now is not None else time.time()
    canon_names = canon_names or {}
    existing = {r[0] for r in con.execute("SELECT video_id FROM song_cards")}
    from music_memory import extract_video_id
    from song_name_clean import dj_display_name

    queue = select_card_queue(songs, existing, limit=CARD_BATCH_LIMIT)
    built = 0
    failures = 0
    for vid, entry in queue:
        canon = canon_names.get(vid)
        if canon:
            artist, title = canon
        else:
            # 乾淨歌名／歌手（同 _try_associative_pick 給 LLM 看的顯示名；原始 title 是 YouTube 標題、帶 MV/HD 雜訊）
            try:
                title, artist = dj_display_name(entry, extract_vid=extract_video_id)
            except Exception:
                title, artist = "", ""
            title = title or str(entry.get("title", "") or "")
            artist = artist or str(entry.get("artist", "") or "")
        try:
            sys_p, user_p = build_card_prompt(artist, title)
            raw = await _analyze_or_raise(router, user_p, caller="theme_link_card", system=sys_p)
            card = parse_song_card(raw, now)
            if card is None:
                raise LLMCallFailed("theme_link_card_parse")
            failures = 0
            con.execute(
                "INSERT OR REPLACE INTO song_cards "
                "(video_id, artist, title, imagery, deep_story, relation, weight, familiarity, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (vid, artist, title, card["imagery"], card["deep_story"], card["relation"],
                 card["weight"], card["familiarity"], card["created_at"]),
            )
            con.commit()
            built += 1
        except LLMCallFailed:
            failures += 1
            logger.warning(f"🧭 [ThemeLink] 建卡 LLM 沒回應（不寫入、下次重試）: {artist} {title}")
            if failures >= MAX_CONSECUTIVE_LLM_FAILURES:
                logger.warning(f"🧭 [ThemeLink] 建卡連續 {failures} 次失敗，本次停止建卡")
                break
        except Exception:
            logger.exception(f"🧭 [ThemeLink] 建卡失敗，跳過: {vid} {artist} {title}")
        await sleep_fn(LLM_SLEEP_S)
    logger.info(f"🧭 [ThemeLink] 歌卡批次完成：新建 {built}/{len(queue)}")
    return built


# ── 2–5. tag 收集／heard_by／釋義＋敏感／房間把關 ───────────────────────────

async def _compute_room_veto(con: sqlite3.Connection, router, tag: str, last_seen: float,
                             guild_id: int, *, sleep_fn=asyncio.sleep) -> bool | None:
    window_start = last_seen - ROOM_VETO_LOOKBACK_MIN * 60
    rows = con.execute(
        "SELECT text FROM transcripts WHERE guild_id=? AND timestamp>=? AND timestamp<? "
        "AND speaker != 'Marvin' ORDER BY timestamp",
        (guild_id, window_start, last_seen),
    ).fetchall()
    lines = [r[0] for r in rows if r[0]]
    if len(lines) < ROOM_VETO_MIN_LINES:
        return None
    sys_p, user_p = build_room_veto_prompt(tag, lines)
    raw = await _analyze_or_raise(router, user_p, caller="theme_link_veto", system=sys_p)
    await sleep_fn(LLM_SLEEP_S)
    return parse_room_veto(raw)


async def run_theme_phase(con: sqlite3.Connection, router, entries, events: list[dict], *,
                          now: float | None = None, sleep_fn=asyncio.sleep, guild_id: int = 0) -> int:
    now = now if now is not None else time.time()
    occurrences = collect_tag_occurrences(entries, now)
    eligible = eligible_tags(occurrences)
    updated = 0
    failures = 0

    for tag in sorted(eligible):
        if failures >= MAX_CONSECUTIVE_LLM_FAILURES:
            logger.warning(f"🧭 [ThemeLink] tag 階段連續 {failures} 次 LLM 失敗，本次停止")
            break
        timestamps = occurrences[tag]
        first_seen = min(timestamps)
        last_seen = max(timestamps)
        day_count = len({tpe_date_str(ts) for ts in timestamps})

        row = con.execute(
            "SELECT meaning, sensitive, first_seen, last_seen, songable FROM theme_tags WHERE tag=?", (tag,)
        ).fetchone()

        meaning, sensitive, songable = (row[0], bool(row[1]), bool(row[4])) if row else (None, None, None)
        if row is None:
            try:
                sys_p, user_p = build_theme_meaning_prompt(tag)
                raw = await _analyze_or_raise(router, user_p, caller="theme_link_meaning", system=sys_p)
                parsed = parse_theme_meaning(raw)
                if parsed is None:
                    raise LLMCallFailed("theme_link_meaning_parse")
                failures = 0
            except LLMCallFailed:
                failures += 1
                logger.warning(f"🧭 [ThemeLink] 釋義 LLM 沒回應（不寫入、下次重試）: {tag}")
                await sleep_fn(LLM_SLEEP_S)
                continue
            except Exception:
                logger.exception(f"🧭 [ThemeLink] 釋義 LLM 失敗，跳過: {tag}")
                continue
            meaning, sensitive, songable = parsed["meaning"], parsed["sensitive"], parsed["songable"]
            await sleep_fn(LLM_SLEEP_S)
            # 新 tag：first_seen 已是本次窗的最早時間，不用再覆寫
        elif last_seen <= row[3]:
            continue  # last_seen 沒更新 → 不重算 heard_by/room_veto
        else:
            first_seen = row[2]  # 既有 tag：first_seen 不變

        heard_by = compute_heard_by(timestamps, events)
        try:
            room_veto = await _compute_room_veto(con, router, tag, last_seen, guild_id, sleep_fn=sleep_fn)
        except LLMCallFailed:
            # 不寫入整筆 tag：執行時沒有這列＝不合格（一樣安全），下次批次重算
            failures += 1
            logger.warning(f"🧭 [ThemeLink] 房間把關 LLM 沒回應（本 tag 不寫入、下次重試）: {tag}")
            continue
        except Exception:
            logger.exception(f"🧭 [ThemeLink] 房間把關 LLM 失敗，視為擋: {tag}")
            room_veto = True

        con.execute(
            "INSERT OR REPLACE INTO theme_tags "
            "(tag, meaning, sensitive, songable, room_veto, heard_by, first_seen, last_seen, day_count, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (tag, meaning, int(sensitive), int(songable), (None if room_veto is None else int(room_veto)),
             json.dumps(heard_by, ensure_ascii=False), first_seen, last_seen, day_count, now),
        )
        con.commit()
        updated += 1

    logger.info(f"🧭 [ThemeLink] tag 釋義/heard_by/房間把關完成：{updated} 筆更新（合格 tag 共 {len(eligible)} 個）")
    return updated


# ── 6. 配歌（粗篩）───────────────────────────────────────────────────────

def _load_cards_with_total_plays(con: sqlite3.Connection, songs: dict) -> dict[str, dict]:
    from music_memory import extract_video_id

    cards: dict[str, dict] = {}
    for vid, artist, title, imagery, deep_story, relation, weight, familiarity in con.execute(
        "SELECT video_id, artist, title, imagery, deep_story, relation, weight, familiarity FROM song_cards"
    ):
        cards[vid] = {
            "video_id": vid, "artist": artist, "title": title, "imagery": imagery,
            "deep_story": deep_story, "relation": relation, "weight": weight,
            "familiarity": familiarity, "total_plays": 0,
        }
    for entry in songs.values():
        if not isinstance(entry, dict):
            continue
        vid = extract_video_id(entry.get("webpage_url") or entry.get("url") or "") or ""
        if vid in cards:
            cards[vid]["total_plays"] = entry.get("total_plays", 0) or 0
    return cards


async def run_match_phase(con: sqlite3.Connection, router, songs: dict, *, sleep_fn=asyncio.sleep) -> int:
    rows = con.execute(
        "SELECT tag, meaning FROM theme_tags WHERE songable=1 AND sensitive=0 "
        "AND (room_veto IS NULL OR room_veto=0)"
    ).fetchall()
    cards_by_vid = _load_cards_with_total_plays(con, songs)

    matched_tags = 0
    for tag, meaning in rows:
        already = con.execute("SELECT COUNT(*) FROM theme_picks WHERE tag=?", (tag,)).fetchone()[0]
        if already > 0:
            continue  # tag 的 meaning 不變就不重算（這裡 meaning 建立後不會再變）

        candidates = select_match_candidates(list(cards_by_vid.values()))
        if not candidates:
            continue  # 候選為空 → 不呼叫 LLM（盲測：空候選會被 LLM 自己編歌）

        numbered = list(enumerate(candidates))
        try:
            sys_p, user_p = build_theme_match_prompt(tag, meaning, numbered)
            raw = await _analyze_or_raise(router, user_p, caller="theme_link_match", system=sys_p)
            picks = parse_theme_picks(raw, n_cards=len(candidates))
        except LLMCallFailed:
            logger.warning(f"🧭 [ThemeLink] 配歌 LLM 沒回應（不寫入、下次重試）: {tag}")
            await sleep_fn(LLM_SLEEP_S)
            continue
        except Exception:
            logger.exception(f"🧭 [ThemeLink] 配歌 LLM 失敗，跳過: {tag}")
            continue
        await sleep_fn(LLM_SLEEP_S)

        con.execute("DELETE FROM theme_picks WHERE tag=?", (tag,))
        for rank, (idx, layer) in enumerate(picks):
            vid = candidates[idx]["video_id"]
            con.execute(
                "INSERT OR REPLACE INTO theme_picks (tag, video_id, rank, layer) VALUES (?,?,?,?)",
                (tag, vid, rank, layer),
            )
        con.commit()
        if picks:
            matched_tags += 1

    logger.info(f"🧭 [ThemeLink] 配歌批次完成：{matched_tags}/{len(rows)} 個合格 tag 選到歌")
    return matched_tags


# ── main ─────────────────────────────────────────────────────────────────

async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    from chat_summary_parser import parse_log
    from llm_pool import build_tiered_router
    from music_memory import MusicMemory

    router = build_tiered_router(env={"GOOGLE_API_KEY": os.environ["GOOGLE_API_KEY"], **BATCH_MODEL_OVERRIDES})
    con = sqlite3.connect(DB_PATH)
    try:
        init_tables(con)
        mm = MusicMemory(MUSIC_MEMORY_PATH)
        songs = mm.all_songs()
        canon_names = load_canon_names(SONG_KNOWLEDGE_PATH)

        await run_card_phase(con, router, songs, canon_names=canon_names)

        text = CHAT_LOG_PATH.read_text(encoding="utf-8") if CHAT_LOG_PATH.exists() else ""
        entries = parse_log(text)
        events = load_presence_events()
        if not events:
            logger.warning("🧭 [ThemeLink] voice_presence 讀不到任何事件，所有 tag 的 heard_by 會是空的（不會觸發）")
        guild_id = int(os.environ.get("GUILD_ID") or "0")
        if guild_id == 0:
            # 逐字稿存在真正的 guild id 底下；0 會讓房間把關永遠撈不到內容、等於默默關掉
            logger.warning("🧭 [ThemeLink] GUILD_ID 未設定，房間把關撈不到逐字稿，本次不跑 tag 階段")
            return
        await run_theme_phase(con, router, entries, events, guild_id=guild_id)

        await run_match_phase(con, router, songs)
    finally:
        con.close()


if __name__ == "__main__":
    asyncio.run(main())

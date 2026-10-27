"""theme_link.py — 主題連結 MVP 的純函式層。

房間 14 天內一再聊到的話題（10 分鐘日誌的 meme tag），離線批次配好歌，
執行時併進 Associative 選曲（見 docs/designs/theme-link-mvp.md）。

這個模組只放純函式：tag 正規化、合格 tag、presence 重建 heard_by、
在場閘門、LLM 輸出解析（card / meaning / picks）、prompt 組裝、候選篩選。
不做檔案 IO、不做 DB 讀寫、不呼叫 LLM——那些留給 scripts/build_theme_links.py
與 cogs/music_cog_autopilot.py。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

TPE_TZ = timezone(timedelta(hours=8))

LOOKBACK_DAYS = 14.0
CARD_BATCH_LIMIT = 150
MATCH_POOL = 120
ROOM_VETO_MIN_LINES = 5
ROOM_VETO_LOOKBACK_MIN = 15

_TAG_SPLIT_RE = re.compile(r"[、/，,]")
_TAG_BLANK_VALUES = {"", "-", "—", "無"}


# ── 1. tag 正規化／合格 tag（批次第 2 步）────────────────────────────────────

def normalize_tags(raw: str) -> list[str]:
    """去空白；用「、」「/」「，」拆成多個 tag；丟掉長度 < 2 的與空白值。"""
    if not raw:
        return []
    out: list[str] = []
    for part in _TAG_SPLIT_RE.split(raw):
        tag = re.sub(r"\s+", "", part)
        if tag in _TAG_BLANK_VALUES:
            continue
        if len(tag) < 2:
            continue
        out.append(tag)
    return out


def parse_tpe_timestamp(ts_str: str) -> float:
    """日誌時間戳字串（台灣時間）→ epoch。"""
    return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TPE_TZ).timestamp()


def tpe_date_str(ts: float) -> str:
    """epoch → 台灣時間的 YYYY-MM-DD。"""
    return datetime.fromtimestamp(ts, TPE_TZ).strftime("%Y-%m-%d")


def collect_tag_occurrences(entries, now: float, window_days: float = LOOKBACK_DAYS) -> dict[str, list[float]]:
    """從 DiaryEntry 清單（需有 .ts_str / .meme_id）收集近 window_days 天內每個 tag 的出現時間戳。

    時間戳即該窗的日誌時間戳（視為窗結束時間，見 heard_by 窗重建）。ts_str 解析失敗的條目跳過。
    """
    cutoff = now - window_days * 86400
    result: dict[str, list[float]] = {}
    for entry in entries:
        meme_id = getattr(entry, "meme_id", "") or ""
        if not meme_id:
            continue
        try:
            ts = parse_tpe_timestamp(entry.ts_str)
        except (ValueError, AttributeError):
            continue
        if ts < cutoff:
            continue
        for tag in normalize_tags(meme_id):
            result.setdefault(tag, []).append(ts)
    return result


def eligible_tags(occurrences: dict[str, list[float]]) -> set[str]:
    """合格 tag：在 ≥ 2 個不同（台灣時間）日期出現。"""
    out: set[str] = set()
    for tag, timestamps in occurrences.items():
        dates = {tpe_date_str(ts) for ts in timestamps}
        if len(dates) >= 2:
            out.add(tag)
    return out


# ── 2. heard_by（presence 重建，批次第 3 步）────────────────────────────────

def reconstruct_present_users(events: list[dict], window_start: float, window_end: float) -> set[str]:
    """重建 [window_start, window_end) 內任何時刻在場的非 bot 使用者（user_name）。

    events：presence_logger.py 寫的 join/leave 紀錄（ts、user_name、event、is_bot）。
    join 在窗前、leave 在窗後或窗內離開 → 在場；leave 在窗開始前（含等於）→ 不在場；
    窗內 join → 在場；bot 不算。
    """
    by_user: dict[str, list[tuple[float, str]]] = {}
    for ev in events:
        if ev.get("is_bot"):
            continue
        name = ev.get("user_name")
        ts = ev.get("ts")
        kind = ev.get("event")
        if not name or ts is None or kind not in ("join", "leave"):
            continue
        by_user.setdefault(name, []).append((float(ts), kind))

    present: set[str] = set()
    for name, evs in by_user.items():
        evs.sort(key=lambda e: e[0])
        joined = False
        for ts, kind in evs:
            if ts >= window_end:
                break
            if kind == "join":
                if ts < window_start:
                    joined = True
                else:
                    present.add(name)
                    joined = True
            elif kind == "leave":
                if ts <= window_start:
                    joined = False
                elif joined:
                    present.add(name)
                    joined = False
        if joined:
            present.add(name)
    return present


def presence_gate_open(members: list[str], heard_by) -> bool:
    """members ⊆ heard_by 才過；members 空 → 不過。"""
    if not members:
        return False
    heard_set = set(heard_by)
    return all(m in heard_set for m in members)


# ── 3. LLM 輸出解析（批次第 1/4/5/6 步）─────────────────────────────────────

def _extract_json_obj(raw_text) -> dict | None:
    if not raw_text or not isinstance(raw_text, str):
        return None
    m = re.search(r"\{.*\}", raw_text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def parse_song_card(raw_text, now: float) -> dict | None:
    """解析歌卡 LLM 輸出。JSON 解析不出來 → None（沒有結果，不寫入）。
    deep_story 缺漏/「不確定」→ familiarity='低'、deep_story=''（模型真的不熟，避免每晚重打同一首）。"""
    data = _extract_json_obj(raw_text)
    if data is None:
        return None
    fallback = {"imagery": "", "deep_story": "", "relation": "", "weight": "輕",
                "familiarity": "低", "created_at": now}
    deep_story = str(data.get("deep_story", "")).strip()
    if deep_story in ("", "不確定"):
        return fallback
    imagery = str(data.get("imagery", "")).strip()
    relation = str(data.get("relation", "")).strip()
    weight = str(data.get("weight", "")).strip()
    if weight not in ("輕", "中", "重"):
        weight = "輕"
    familiarity = str(data.get("familiarity", "")).strip()
    if familiarity not in ("高", "中", "低"):
        familiarity = "低"
    return {"imagery": imagery, "deep_story": deep_story, "relation": relation,
            "weight": weight, "familiarity": familiarity, "created_at": now}


def parse_theme_meaning(raw_text) -> dict | None:
    """解析 tag 釋義 LLM 輸出。JSON 解析不出來、或 meaning 空 → None（沒有結果，不寫入）。
    sensitive 缺漏或非 bool → 視為 True。songable 缺漏或非 bool → 視為 False（保守：不配歌）。"""
    data = _extract_json_obj(raw_text)
    if data is None:
        return None
    meaning = str(data.get("meaning", "")).strip()
    if not meaning:
        return None
    sensitive = data.get("sensitive")
    if not isinstance(sensitive, bool):
        sensitive = True
    songable = data.get("songable")
    if not isinstance(songable, bool):
        songable = False
    return {"meaning": meaning, "sensitive": sensitive, "songable": songable}


def parse_room_veto(raw_text) -> bool:
    """解析房間把關 LLM 輸出。解析失敗或欄位缺漏/非 bool → 視為 True（擋）。"""
    data = _extract_json_obj(raw_text)
    if data is None:
        return True
    veto = data.get("touches_sensitive")
    if not isinstance(veto, bool):
        return True
    return veto


def parse_theme_picks(raw_text, n_cards: int) -> list[tuple[int, str]]:
    """解析配歌 LLM 輸出。回 [(編號, layer)]；編號超出範圍丟掉；最多 5 個。"""
    data = _extract_json_obj(raw_text)
    if data is None:
        return []
    picks = data.get("picks")
    if not isinstance(picks, list):
        return []
    out: list[tuple[int, str]] = []
    for item in picks:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        if not isinstance(idx, int) or isinstance(idx, bool) or not (0 <= idx < n_cards):
            continue
        layer = str(item.get("layer", "")).strip()
        if layer not in ("意象", "深層", "兩者"):
            layer = "意象"
        out.append((idx, layer))
        if len(out) >= 5:
            break
    return out


# ── 4. 候選篩選（批次第 1/6 步）──────────────────────────────────────────────

def select_card_queue(songs: dict, existing_video_ids, *, min_total_plays: int = 2,
                       limit: int = CARD_BATCH_LIMIT) -> list[tuple[str, dict]]:
    """排隊建卡：total_plays >= min_total_plays，依 total_plays 由高到低，跳過已有卡／取不到
    video_id 的歌。回 [(video_id, entry)]，最多 limit 筆。"""
    from music_memory import extract_video_id

    existing = set(existing_video_ids)
    candidates: list[tuple[str, dict, float]] = []
    for entry in songs.values():
        if not isinstance(entry, dict):
            continue
        total_plays = entry.get("total_plays")
        if not isinstance(total_plays, (int, float)) or total_plays < min_total_plays:
            continue
        webpage_url = entry.get("webpage_url") or entry.get("url") or ""
        vid = extract_video_id(webpage_url) or ""
        if not vid or vid in existing:
            continue
        candidates.append((vid, entry, total_plays))
    candidates.sort(key=lambda c: c[2], reverse=True)
    return [(vid, entry) for vid, entry, _ in candidates[:limit]]


def select_match_candidates(cards: list[dict], *, limit: int = MATCH_POOL) -> list[dict]:
    """配歌候選：有卡、familiarity in (高,中)、weight != 重，依 total_plays 排序取前 limit。"""
    eligible = [c for c in cards if c.get("familiarity") in ("高", "中") and c.get("weight") != "重"]
    eligible.sort(key=lambda c: c.get("total_plays", 0), reverse=True)
    return eligible[:limit]


def merge_theme_picks_into_local(theme_entries: list[tuple[str, dict]],
                                  local_cands: list[tuple[str, dict]],
                                  limit: int) -> list[tuple[str, dict]]:
    """theme 挑選排最前面、local_cands 接續，依 display 字串去重，總數截到 limit
    （超過時從尾巴砍）。"""
    seen: set[str] = set()
    merged: list[tuple[str, dict]] = []
    for display, entry in list(theme_entries) + list(local_cands):
        if display in seen:
            continue
        seen.add(display)
        merged.append((display, entry))
    return merged[:limit]


# ── 5. 執行時合格 tag 判定（B 段步驟 1）─────────────────────────────────────

def qualifying_theme_tags(theme_rows: list[dict], members: list[str], now: float,
                           used_tags_14d, used_tag_today: bool,
                           window_days: float = LOOKBACK_DAYS, max_tags: int = 3) -> list[tuple[str, str]]:
    """回最多 max_tags 個 (tag, meaning)，依 last_seen 新到舊。

    today 已有一筆 source=="tag" → []；members 空 → []；theme_rows 的每一列需
    last_seen 在 window_days 內、sensitive is False、songable is True、room_veto 不是 True、
    members ⊆ heard_by、tag 不在 used_tags_14d 內。
    """
    if used_tag_today or not members:
        return []
    member_set = set(members)
    used = set(used_tags_14d)
    cutoff = now - window_days * 86400

    candidates = []
    for row in theme_rows:
        if row.get("last_seen", 0) < cutoff:
            continue
        if row.get("sensitive") is not False:
            continue
        if row.get("songable") is not True:
            continue
        if row.get("room_veto") is True:
            continue
        heard = set(row.get("heard_by") or [])
        if not member_set.issubset(heard):
            continue
        tag = row.get("tag", "")
        if not tag or tag in used:
            continue
        candidates.append(row)

    candidates.sort(key=lambda r: r.get("last_seen", 0), reverse=True)
    return [(r["tag"], r.get("meaning", "")) for r in candidates[:max_tags]]


# ── 6. Prompt 組裝（批次第 1/4/5/6 步）──────────────────────────────────────

_CARD_SYS_PROMPT = (
    "你是音樂知識庫。針對一首歌，憑你自己的知識寫這首歌的「故事卡」——不要搜尋、不要編造。"
    "不熟悉這首歌就老實寫「不確定」，不准瞎掰創作背景。只回傳 JSON：\n"
    '{"imagery":"意象（地名/場景/動作，20字內）",'
    '"deep_story":"創作背景或深層故事（一兩句；不確定就填「不確定」）",'
    '"relation":"這首歌適合呼應的人際/生活關係（10字內）",'
    '"weight":"情緒重量：重＝死亡、喪親、重病、自殺、戰爭離散等沉重題材；一般失戀、思念、遺憾＝中；輕快日常＝輕。只回 輕|中|重",'
    '"familiarity":"你對這首歌的熟悉程度，高|中|低"}'
)


def build_card_prompt(artist: str, title: str) -> tuple[str, str]:
    user_prompt = f"歌手：{artist or '（不詳）'}\n歌名：{title}\n請寫這首歌的故事卡，回傳 JSON。"
    return _CARD_SYS_PROMPT, user_prompt


_MEANING_SYS_PROMPT = (
    "你是語言學助理。針對一個中文話題 tag：\n"
    "1. 寫出「字典本義＋延伸義」（一兩句）。\n"
    "2. 判斷這個話題有沒有生活或情感層面、能用一首歌的故事去呼應（songable）。\n"
    "   日常生活經驗、人生階段、人際關係、地方與移動 → true（例：露營、搬家、養狗、旅行、宵夜）；\n"
    "   純遊戲機制、技術故障、軟硬體規格、工作流程細節 → false（例：bug、裝備升級、登入問題、遊戲攻略、開發板）。\n"
    "3. 判斷是否觸及健康、喪親、感情、衝突、金錢糾紛等敏感事（sensitive）。\n"
    "只回傳 JSON：{\"meaning\":\"字典本義；延伸：...\",\"songable\":true或false,\"sensitive\":true或false}"
)


def build_theme_meaning_prompt(tag: str) -> tuple[str, str]:
    user_prompt = f"話題：{tag}\n請給釋義與敏感判斷，回傳 JSON。"
    return _MEANING_SYS_PROMPT, user_prompt


_ROOM_VETO_SYS_PROMPT = (
    "你在檢查一段 Discord 語音頻道逐字稿，判斷這段對話是否觸及健康、喪親、感情、衝突等敏感事。"
    '只回傳 JSON：{"touches_sensitive":true或false}，不要附加其他說明或引用原文內容。'
)


def build_room_veto_prompt(tag: str, transcript_lines: list[str]) -> tuple[str, str]:
    body = "\n".join(transcript_lines)
    user_prompt = f"話題：{tag}\n逐字稿：\n{body}\n請判斷，回傳 JSON。"
    return _ROOM_VETO_SYS_PROMPT, user_prompt


_MATCH_SYS_PROMPT = (
    "你是電台選曲者。根據一個話題的釋義，從下面編號過的歌卡清單中，挑出「故事層」真的呼應這個話題的歌"
    "（最多 5 首）——看歌卡的意象與深層故事是否呼應話題的本義或延伸義，不准只靠字面，也不准牽強附會。"
    "寧缺勿濫：沒有真的呼應的歌就回空陣列 {\"picks\":[]}，這是完全正常的結果。"
    "只能用清單裡的編號，絕對不准自己寫歌名。只回傳 JSON：\n"
    '{"picks":[{"index":編號,"layer":"意象或深層或兩者"}, ...]}'
)


def build_theme_match_prompt(tag: str, meaning: str, numbered_cards: list[tuple[int, dict]]) -> tuple[str, str]:
    lines = [
        f"{i}. {c.get('artist', '')} - {c.get('title', '')}｜"
        f"意象：{c.get('imagery', '')[:40]}｜故事：{c.get('deep_story', '')[:40]}"
        for i, c in numbered_cards
    ]
    user_prompt = (
        f"話題：{tag}\n釋義：{meaning}\n歌卡清單：\n" + "\n".join(lines) +
        "\n請選最多 5 首最呼應的歌，回傳 JSON。"
    )
    return _MATCH_SYS_PROMPT, user_prompt

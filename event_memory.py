"""event_memory.py

事件記憶 Phase 1——從 5 分鐘摘要窗口抽出生活事件、跨窗合併，供 EventRecallAgent
問答；回答只用 DB 欄位組句（不讓 LLM 自由生成事實）。
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
import sqlite3
import time

from rapidfuzz import fuzz

from music_fastpath import to_pinyin

logger = logging.getLogger(__name__)

TPE_TZ = datetime.timezone(datetime.timedelta(hours=8))
RETENTION_DAYS = 30
RECENT_DAYS = 7
RECENT_LIMIT = 10
SELECT_CANDIDATE_LIMIT = 15
MAX_QUOTES = 5
QUOTE_SPEAK_MAX = 30
ACTIVITY_MAX_LEN = 12
DATE_PAST_DAYS = 60
DATE_FUTURE_DAYS = 365
MERGE_DATE_TOLERANCE_DAYS = 1
MERGE_NULL_DATE_WINDOW_S = 3 * 3600
MERGE_PINYIN_THRESHOLD = 80
EXTRACT_TIMEOUT_S = 20.0
EXTRACT_PREFER_PROVIDERS = ("gemini_free", "groq")
NOT_REMEMBER = "我不記得有聽過這件事。"
_MARVIN_NAMES = {"馬文", "marvin"}
_SELF_SUBJECTS = {"我", "我們", "我自己"}
HOMETOWN_KEYWORDS = ("老家", "家鄉", "故鄉")
FOLLOWUP_MAX_DAYS_AFTER = 14


class EventStore:
    def __init__(self, db_path: str = "marvin.db"):
        self._db_path = db_path
        self._con: sqlite3.Connection | None = (
            sqlite3.connect(":memory:", check_same_thread=False) if db_path == ":memory:" else None
        )
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        if self._con is not None:
            return self._con
        return sqlite3.connect(self._db_path)

    def _release(self, con: sqlite3.Connection) -> None:
        if self._con is None:
            con.close()

    def _init_db(self) -> None:
        con = self._connect()
        try:
            con.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id     INTEGER NOT NULL,
                    owner        TEXT    NOT NULL,
                    activity     TEXT    NOT NULL,
                    place        TEXT,
                    event_date   REAL,
                    date_text    TEXT    NOT NULL DEFAULT '',
                    participants TEXT    NOT NULL DEFAULT '[]',
                    heard_by     TEXT    NOT NULL DEFAULT '[]',
                    quotes       TEXT    NOT NULL DEFAULT '[]',
                    first_seen   REAL    NOT NULL,
                    last_seen    REAL    NOT NULL,
                    created_at   REAL    NOT NULL
                )
            """)
            con.execute("""
                CREATE INDEX IF NOT EXISTS idx_events_guild_owner_seen
                ON events (guild_id, owner, last_seen)
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS hometowns (
                    guild_id   INTEGER NOT NULL,
                    person     TEXT    NOT NULL,
                    place      TEXT    NOT NULL,
                    quote      TEXT    NOT NULL DEFAULT '',
                    learned_at REAL    NOT NULL,
                    PRIMARY KEY (guild_id, person)
                )
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS event_followups (
                    event_id       INTEGER PRIMARY KEY,
                    followed_up_at REAL    NOT NULL
                )
            """)
            con.commit()
        finally:
            self._release(con)

    def add(self, guild_id: int, ev: dict, heard_by: list[str], seen_ts: float) -> int:
        seen_heard_by = []
        for name in heard_by:
            if name not in seen_heard_by:
                seen_heard_by.append(name)
        quote = (ev.get("quote") or "").strip()
        quotes = [quote] if quote else []
        con = self._connect()
        try:
            cur = con.execute(
                """INSERT INTO events
                   (guild_id, owner, activity, place, event_date, date_text,
                    participants, heard_by, quotes, first_seen, last_seen, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    guild_id, ev["owner"], ev["activity"], ev.get("place"),
                    ev.get("event_date"), ev.get("date_text") or "",
                    json.dumps(ev.get("participants") or [], ensure_ascii=False),
                    json.dumps(seen_heard_by, ensure_ascii=False),
                    json.dumps(quotes, ensure_ascii=False),
                    seen_ts, seen_ts, time.time(),
                ),
            )
            con.commit()
            return cur.lastrowid
        finally:
            self._release(con)

    def get(self, event_id: int) -> dict | None:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT id, guild_id, owner, activity, place, event_date, date_text, "
                "participants, heard_by, quotes, first_seen, last_seen, created_at "
                "FROM events WHERE id = ?",
                (event_id,),
            ).fetchone()
            return self._row_to_dict(row) if row else None
        finally:
            self._release(con)

    def merge(self, event_id: int, ev: dict, heard_by: list[str], seen_ts: float) -> None:
        existing = self.get(event_id)
        if existing is None:
            return

        new_heard_by = list(existing["heard_by"])
        for name in heard_by:
            if name not in new_heard_by:
                new_heard_by.append(name)

        new_participants = list(existing["participants"])
        for name in ev.get("participants") or []:
            if name not in new_participants:
                new_participants.append(name)

        quotes = list(existing["quotes"])
        quote = (ev.get("quote") or "").strip()
        if quote and quote not in quotes:
            quotes.append(quote)
        quotes = quotes[-MAX_QUOTES:]

        place = ev.get("place") if ev.get("place") is not None else existing["place"]
        event_date = ev.get("event_date") if ev.get("event_date") is not None else existing["event_date"]
        date_text = ev.get("date_text") if (ev.get("date_text") or "") else existing["date_text"]
        last_seen = max(existing["last_seen"], seen_ts)

        con = self._connect()
        try:
            con.execute(
                """UPDATE events SET place=?, event_date=?, date_text=?, participants=?,
                   heard_by=?, quotes=?, last_seen=? WHERE id=?""",
                (
                    place, event_date, date_text,
                    json.dumps(new_participants, ensure_ascii=False),
                    json.dumps(new_heard_by, ensure_ascii=False),
                    json.dumps(quotes, ensure_ascii=False),
                    last_seen, event_id,
                ),
            )
            con.commit()
        finally:
            self._release(con)

    def recent_for_owners(self, guild_id: int, owners: list[str], since_ts: float, limit: int = RECENT_LIMIT) -> list[dict]:
        if not owners:
            return []
        con = self._connect()
        try:
            placeholders = ",".join("?" * len(owners))
            rows = con.execute(
                f"SELECT id, guild_id, owner, activity, place, event_date, date_text, "
                f"participants, heard_by, quotes, first_seen, last_seen, created_at "
                f"FROM events WHERE guild_id = ? AND owner IN ({placeholders}) AND last_seen >= ? "
                f"ORDER BY last_seen DESC LIMIT ?",
                (guild_id, *owners, since_ts, limit),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            self._release(con)

    def list_guild(self, guild_id: int, since_ts: float) -> list[dict]:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT id, guild_id, owner, activity, place, event_date, date_text, "
                "participants, heard_by, quotes, first_seen, last_seen, created_at "
                "FROM events WHERE guild_id = ? AND last_seen >= ? ORDER BY last_seen DESC",
                (guild_id, since_ts),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            self._release(con)

    def list_followup_candidates(self, guild_id: int) -> list[dict]:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT id, guild_id, owner, activity, place, event_date, date_text, "
                "participants, heard_by, quotes, first_seen, last_seen, created_at "
                "FROM events WHERE guild_id = ? AND event_date IS NOT NULL "
                "AND id NOT IN (SELECT event_id FROM event_followups) "
                "ORDER BY event_date DESC",
                (guild_id,),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            self._release(con)

    def mark_followed_up(self, event_id: int, ts: float) -> None:
        con = self._connect()
        try:
            con.execute(
                "INSERT OR IGNORE INTO event_followups (event_id, followed_up_at) VALUES (?, ?)",
                (event_id, ts),
            )
            con.commit()
        finally:
            self._release(con)

    def prune(self, retention_days: int = RETENTION_DAYS, now: float | None = None) -> int:
        effective_now = time.time() if now is None else now
        cutoff = effective_now - retention_days * 86400
        con = self._connect()
        try:
            cur = con.execute("DELETE FROM events WHERE last_seen < ?", (cutoff,))
            con.execute("DELETE FROM hometowns WHERE learned_at < ?", (cutoff,))
            con.execute("DELETE FROM event_followups WHERE event_id NOT IN (SELECT id FROM events)")
            con.commit()
            return cur.rowcount
        finally:
            self._release(con)

    def set_hometown(self, guild_id: int, person: str, place: str, quote: str, learned_at: float) -> None:
        con = self._connect()
        try:
            con.execute(
                """INSERT INTO hometowns (guild_id, person, place, quote, learned_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(guild_id, person) DO UPDATE SET
                       place=excluded.place, quote=excluded.quote, learned_at=excluded.learned_at""",
                (guild_id, person, place, quote, learned_at),
            )
            con.commit()
        finally:
            self._release(con)

    def get_hometowns(self, guild_id: int, persons: list[str]) -> dict[str, str]:
        if not persons:
            return {}
        con = self._connect()
        try:
            placeholders = ",".join("?" * len(persons))
            rows = con.execute(
                f"SELECT person, place FROM hometowns WHERE guild_id = ? AND person IN ({placeholders})",
                (guild_id, *persons),
            ).fetchall()
            return {person: place for person, place in rows}
        finally:
            self._release(con)

    @staticmethod
    def _row_to_dict(row: tuple) -> dict:
        return {
            "id": row[0],
            "guild_id": row[1],
            "owner": row[2],
            "activity": row[3],
            "place": row[4],
            "event_date": row[5],
            "date_text": row[6],
            "participants": json.loads(row[7]),
            "heard_by": json.loads(row[8]),
            "quotes": json.loads(row[9]),
            "first_seen": row[10],
            "last_seen": row[11],
            "created_at": row[12],
        }


def _normalize(text: str) -> str:
    """去空白與標點、轉小寫，用於「是否真的出現在逐字稿」的子字串比對。"""
    return re.sub(r"[\W_]+", "", text or "").lower()


def _strip_code_fence(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        elif lines and lines[-1].strip().endswith("```"):
            lines[-1] = lines[-1].rsplit("```", 1)[0]
        text = "\n".join(lines)
    return text


def parse_events(content: str) -> list[dict]:
    try:
        data = json.loads(_strip_code_fence(content))
        if not isinstance(data, dict):
            return []
        events = data.get("events")
        if not isinstance(events, list):
            return []
        return [e for e in events if isinstance(e, dict)]
    except Exception:
        return []


def parse_hometowns(content: str) -> list[dict]:
    try:
        data = json.loads(_strip_code_fence(content))
        if not isinstance(data, dict):
            return []
        hometowns = data.get("hometowns")
        if not isinstance(hometowns, list):
            return []
        return [h for h in hometowns if isinstance(h, dict)]
    except Exception:
        return []


def validate_event(
    raw: dict, heard_by: list[str], transcript_text: str, window_end: float,
    hometowns: dict[str, str] | None = None,
) -> dict | None:
    owner = str(raw.get("owner") or "").strip()
    if not owner or owner not in heard_by or owner.lower() in _MARVIN_NAMES:
        return None

    activity = str(raw.get("activity") or "").strip()
    if not activity or len(activity) > ACTIVITY_MAX_LEN:
        return None

    event_date = None
    date_raw = raw.get("date")
    if isinstance(date_raw, str):
        try:
            d = datetime.date.fromisoformat(date_raw)
        except ValueError:
            d = None
        if d is not None:
            window_day = datetime.datetime.fromtimestamp(window_end, TPE_TZ).date()
            lo = window_day - datetime.timedelta(days=DATE_PAST_DAYS)
            hi = window_day + datetime.timedelta(days=DATE_FUTURE_DAYS)
            if lo <= d <= hi:
                event_dt = datetime.datetime(d.year, d.month, d.day, tzinfo=TPE_TZ)
                event_date = event_dt.timestamp()

    place = str(raw.get("place") or "").strip()
    if not place or not _normalize(place):
        place = None
    elif _normalize(place) not in _normalize(transcript_text) and not _same_place(
        place, (hometowns or {}).get(owner)
    ):
        place = None

    quote = str(raw.get("quote") or "").strip()
    if not _normalize(quote) or _normalize(quote) not in _normalize(transcript_text):
        quote = ""

    subject = str(raw.get("subject") or "").strip()
    if subject in _SELF_SUBJECTS:
        speaker = _quote_speaker(quote, transcript_text)
        if not speaker or speaker not in heard_by or speaker.lower() in _MARVIN_NAMES:
            return None
        owner = speaker
    elif subject and subject in heard_by and subject.lower() not in _MARVIN_NAMES:
        owner = subject
    else:
        return None

    date_text = str(raw.get("date_text") or "").strip()[:20]

    participants = []
    raw_participants = raw.get("participants")
    if isinstance(raw_participants, list):
        for p in raw_participants:
            if isinstance(p, str) and p.strip():
                participants.append(p.strip())

    same_as = raw.get("same_as")
    if isinstance(same_as, bool):
        same_as = None
    elif isinstance(same_as, int):
        pass
    elif isinstance(same_as, str) and same_as.strip().isdigit():
        same_as = int(same_as.strip())
    else:
        same_as = None

    return {
        "owner": owner,
        "activity": activity,
        "place": place,
        "event_date": event_date,
        "date_text": date_text,
        "participants": participants,
        "quote": quote,
        "same_as": same_as,
    }


def validate_hometown(raw: dict, heard_by: list[str], transcript_text: str) -> dict | None:
    person = str(raw.get("person") or "").strip()
    if not person or person not in heard_by or person.lower() in _MARVIN_NAMES:
        return None

    place = str(raw.get("place") or "").strip()
    if not _normalize(place):
        return None

    quote = str(raw.get("quote") or "").strip()
    norm_quote = _normalize(quote)
    if not norm_quote or norm_quote not in _normalize(transcript_text):
        return None

    if not any(kw in quote for kw in HOMETOWN_KEYWORDS):
        return None
    if _normalize(place) not in norm_quote:
        return None

    return {"person": person, "place": place, "quote": quote}


_TRANSCRIPT_LINE_RE = re.compile(r"^\[[\d:]+\]\s*(.+?):\s*(.*)$")


def _quote_speaker(quote: str, transcript_text: str) -> str | None:
    """逐字稿裡第一句內容包含 quote 的說話者，找不到回 None。"""
    norm_quote = _normalize(quote)
    if not norm_quote:
        return None
    for line in (transcript_text or "").splitlines():
        m = _TRANSCRIPT_LINE_RE.match(line.strip())
        if not m:
            continue
        speaker, text = m.group(1).strip(), m.group(2).strip()
        if norm_quote in _normalize(text):
            return speaker
    return None


# 本地補漏（LLM 漏抓 9/29 陳進文「所以說我臺東老家那邊」）：只認「我(們)X老家」「我(的)老家在X」兩種自述句型
_HOMETOWN_LOCAL_PATTERNS = (
    re.compile(r"我們?([\u4e00-\u9fff]{2,3}?)(?:老家|家鄉|故鄉)"),
    re.compile(r"我們?的?(?:老家|家鄉|故鄉)在([\u4e00-\u9fff]{2,3}?)(?=$|[^\u4e00-\u9fff]|[啦啊呢喔哦耶吧嗎])"),
)
_HOMETOWN_PLACE_STOPCHARS = set("們的我你他她要回去在那這個是到說了也都就")


def learn_hometowns_local(transcript_text: str, heard_by: list[str]) -> list[dict]:
    """從逐字稿（"[HH:MM:SS] 說話者: 內容" 每行一句）用 regex 抓自述老家，不靠 LLM。
    結果再過 validate_hometown（同一套在場/原話/關鍵詞檢查）；同人同地去重。"""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for line in (transcript_text or "").splitlines():
        m = _TRANSCRIPT_LINE_RE.match(line.strip())
        if not m:
            continue
        speaker, text = m.group(1).strip(), m.group(2).strip()
        for pat in _HOMETOWN_LOCAL_PATTERNS:
            for pm in pat.finditer(text):
                place = pm.group(1)
                if set(place) & _HOMETOWN_PLACE_STOPCHARS:
                    continue
                h = validate_hometown({"person": speaker, "place": place, "quote": text},
                                      heard_by, transcript_text)
                if h is None or (h["person"], h["place"]) in seen:
                    continue
                seen.add((h["person"], h["place"]))
                out.append(h)
    return out


def _activity_similarity(a: str, b: str) -> float:
    pa, pb = to_pinyin(a), to_pinyin(b)
    if pa and pb:
        return fuzz.ratio(pa, pb)
    return fuzz.ratio(a, b)


def _same_place(a: str | None, b: str | None) -> bool:
    """兩邊都有地點且正規化後相同（台/臺視為同字）。"""
    if not a or not b:
        return False
    na, nb = _normalize(a).replace("台", "臺"), _normalize(b).replace("台", "臺")
    return bool(na) and na == nb


def find_merge_target(ev: dict, recent: list[dict], window_end: float) -> int | None:
    candidates = [r for r in recent if r["owner"] == ev["owner"]]

    same_as = ev.get("same_as")
    if same_as is not None:
        for r in candidates:
            if r["id"] == same_as:
                return r["id"]

    for r in candidates:
        if ev["event_date"] is not None and r["event_date"] is not None:
            date_close = abs(ev["event_date"] - r["event_date"]) <= MERGE_DATE_TOLERANCE_DAYS * 86400
        else:
            date_close = (window_end - r["last_seen"]) <= MERGE_NULL_DATE_WINDOW_S

        # 同地也算同一件：「去臺東」vs「回老家（臺東）」活動名不像但是同一趟（2026-10-10 重播 9/29）
        if date_close and (_activity_similarity(ev["activity"], r["activity"]) >= MERGE_PINYIN_THRESHOLD
                           or _same_place(ev.get("place"), r.get("place"))):
            return r["id"]

    return None


def visible_events(events: list[dict], present: list[str]) -> list[dict]:
    if not present:
        return []
    present_set = set(present)
    return [e for e in events if present_set <= set(e["heard_by"])]


def parse_ids(content: str, allowed_ids: set[int]) -> list[int]:
    try:
        data = json.loads(_strip_code_fence(content))
        if not isinstance(data, dict):
            return []
        ids_raw = data.get("ids")
        if not isinstance(ids_raw, list):
            return []
        out: list[int] = []
        for v in ids_raw:
            if isinstance(v, bool):
                continue
            if isinstance(v, int):
                i = v
            elif isinstance(v, str) and v.strip().isdigit():
                i = int(v.strip())
            else:
                continue
            if i in allowed_ids and i not in out:
                out.append(i)
        return out
    except Exception:
        return []


def _md(ts: float) -> str:
    d = datetime.datetime.fromtimestamp(ts, TPE_TZ)
    return f"{d.month}月{d.day}日"


def render_answer(ev: dict, speaker: str) -> str:
    who = "你" if ev["owner"] == speaker else ev["owner"]
    text = f"我記得{who}{_md(ev['first_seen'])}說過要{ev['activity']}"
    # 不念 place：地名在逐字稿出現 ≠ 意思對（10/10 重播把出發地臺中存成地點），只用來選事件
    if ev["event_date"] is not None:
        text += f"，日期是{_md(ev['event_date'])}"
    quote = ev["quotes"][0] if ev["quotes"] else ""
    if quote:
        if len(quote) > QUOTE_SPEAK_MAX:
            quote = quote[:QUOTE_SPEAK_MAX] + "…"
        text += f"。那時說「{quote}」"
    return text + "。"


def pick_followups(events: list[dict], present: list[str], now: float) -> list[dict]:
    """事件過後追問候選（保持輸入順序）：事件當天已過完、未超過 FOLLOWUP_MAX_DAYS_AFTER 天、
    owner 在場、且通過既有 visible_events 隱私濾網（在場者全部 ⊆ heard_by）。"""
    if not present:
        return []
    present_set = set(present)
    visible_ids = {e["id"] for e in visible_events(events, present)}
    out = []
    for e in events:
        if e["event_date"] is None:
            continue
        if now < e["event_date"] + 86400:
            continue
        if now > e["event_date"] + (FOLLOWUP_MAX_DAYS_AFTER + 1) * 86400:
            continue
        if e["owner"] not in present_set:
            continue
        if e["id"] not in visible_ids:
            continue
        out.append(e)
    return out


def render_followup_line(ev: dict) -> str:
    return f"對了{ev['owner']}，你之前說{_md(ev['event_date'])}要{ev['activity']}，後來怎麼樣？"


def render_followup_material(ev: dict) -> str:
    return f"{ev['owner']} 之前說{_md(ev['event_date'])}要{ev['activity']}"


EXTRACT_SYSTEM_PROMPT = """\
你是事件記錄助理。從語音聊天逐字稿中找出「現實生活中、有時間指向的個人事件」：出遊、露營、出國、旅行、看醫生、聚餐、搬家、考試、面試、回老家、參加婚禮等。
不要收：遊戲裡的行動或任務、當下的小動作（去上廁所、去倒水、等一下吃飯）、抱怨與心情、對 AI 助理「馬文」的指令或提問、單純討論新聞或產品、陪家人或朋友看病住院（主角是被陪的人，即使說「我們住雙人房」也不收）。
「近期事件清單」是之前已記下的事件。若逐字稿講的是清單裡的同一件事（即使用字不同、語音辨識有錯字），same_as 填該事件的編號數字；新事件填 null。
date：用「窗口時間」把「明天」「下禮拜六」「上個月」等換算成 YYYY-MM-DD；無法確定填 null。date_text 填原文時間詞。
subject：這件事的主角在原話裡的稱呼，一字不改照抄（例：我、我們、showay、我媽、他）。
owner：subject 是「我」「我們」時填講那句話的說話者；subject 是說話者名稱時填該名稱。主角是家人、朋友、同事、「他」「她」或其他不在說話者名單的人時，這件事不要收。
quote：逐字稿中提到這件事的一句原話，一字不改。
place：只填逐字稿裡出現過的地名原文，沒有填 null。
participants：同行者名稱，沒有填 []。
activity：兩到六個字的活動名，例如 露營、出國、看醫生。
「背景」列出已知的人物老家。有人說「回老家」「回家鄉」時，place 填背景裡這個人的老家地名。
hometowns：若有人在同一句話裡同時說出自己的「老家」「家鄉」或「故鄉」和地名（例：我老家在臺東），列出 {"person":說話者名稱,"place":地名原文,"quote":那句原話一字不改}；沒有填 []。
只輸出 JSON：{"events":[{"same_as":null,"subject":"","owner":"","activity":"","place":null,"date":null,"date_text":"","participants":[],"quote":""}],"hometowns":[]}
沒有事件就輸出 {"events":[],"hometowns":[]}。"""

SELECT_SYSTEM_PROMPT = """\
你是記憶查詢助理。根據使用者的問題，從「事件清單」中挑出問題在問的事件編號。
只能挑清單裡有的編號；清單裡沒有相關事件就回空陣列。不要回答問題本身。
只輸出 JSON：{"ids":[編號]}"""


def build_extract_user_prompt(
    transcript_text: str, recent: list[dict], window_end: float,
    hometowns: dict[str, str] | None = None,
) -> str:
    dt = datetime.datetime.fromtimestamp(window_end, TPE_TZ)
    weekday = "一二三四五六日"[dt.weekday()]
    header = f"窗口時間：{dt.year}-{dt.month:02d}-{dt.day:02d}（星期{weekday}）{dt.hour:02d}:{dt.minute:02d}"
    if hometowns:
        hometown_lines = [f"{person} 老家在{place}" for person, place in hometowns.items()]
    else:
        hometown_lines = ["（無）"]
    if recent:
        lines = [
            f"#{r['id']} {r['owner']}｜{r['activity']}｜{r['place'] or '-'}｜{r['date_text'] or '-'}"
            for r in recent
        ]
    else:
        lines = ["（無）"]
    return (
        f"{header}\n"
        f"背景：\n" + "\n".join(hometown_lines) + "\n"
        f"近期事件清單：\n" + "\n".join(lines) + "\n"
        f"逐字稿：\n{transcript_text}"
    )


def build_select_user_prompt(query: str, speaker: str, events: list[dict]) -> str:
    lines = []
    for e in events:
        date_str = _md(e["event_date"]) if e["event_date"] is not None else "-"
        lines.append(
            f"#{e['id']} {e['owner']}｜{e['activity']}｜{e['place'] or '-'}｜{date_str}｜"
            f"{e['date_text'] or '-'}｜提到於{_md(e['first_seen'])}"
        )
    return (
        f"提問者：{speaker}\n"
        f"問題：{query}\n"
        f"事件清單：\n" + "\n".join(lines)
    )


async def extract_and_store(router, store, guild_id: int, transcript_text: str, heard_by: list[str], window_end: float) -> dict:
    recent = await asyncio.to_thread(store.recent_for_owners, guild_id, heard_by, window_end - RECENT_DAYS * 86400)
    known = await asyncio.to_thread(store.get_hometowns, guild_id, heard_by)
    learned: set[tuple[str, str]] = set()
    # 本地 regex 先學（在 LLM 呼叫前，讓本窗 prompt 背景就有）
    for h in learn_hometowns_local(transcript_text, heard_by):
        await asyncio.to_thread(store.set_hometown, guild_id, h["person"], h["place"], h["quote"], window_end)
        known[h["person"]] = h["place"]
        learned.add((h["person"], _normalize(h["place"]).replace("台", "臺")))
        logger.info(f"🏠 [EventMemory] 學到 {h['person']} 老家在 {h['place']}（本地）")
    content = await asyncio.wait_for(
        router._call_llm(
            EXTRACT_SYSTEM_PROMPT, build_extract_user_prompt(transcript_text, recent, window_end, known),
            is_json=True, tier="simple", temperature=0.2,
            purpose="extract_events", prefer_providers=EXTRACT_PREFER_PROVIDERS,
        ),
        timeout=EXTRACT_TIMEOUT_S,
    )
    stats = {"added": 0, "merged": 0, "dropped": 0, "hometowns": len(learned)}
    for raw in parse_hometowns(content or ""):
        h = validate_hometown(raw, heard_by, transcript_text)
        if h is None or (h["person"], _normalize(h["place"]).replace("台", "臺")) in learned:
            continue
        learned.add((h["person"], _normalize(h["place"]).replace("台", "臺")))
        await asyncio.to_thread(store.set_hometown, guild_id, h["person"], h["place"], h["quote"], window_end)
        known[h["person"]] = h["place"]
        stats["hometowns"] += 1
        logger.info(f"🏠 [EventMemory] 學到 {h['person']} 老家在 {h['place']}")
    for raw in parse_events(content or ""):
        ev = validate_event(raw, heard_by, transcript_text, window_end, known)
        if ev is None:
            stats["dropped"] += 1
            continue
        target = find_merge_target(ev, recent, window_end)
        if target is not None:
            await asyncio.to_thread(store.merge, target, ev, heard_by, window_end)
            stats["merged"] += 1
            logger.info(f"🗓️ [EventMemory] 合併 #{target} ← {ev['owner']}｜{ev['activity']}")
            updated = await asyncio.to_thread(store.get, target)
            if updated:
                recent = [updated if r["id"] == target else r for r in recent]
        else:
            new_id = await asyncio.to_thread(store.add, guild_id, ev, heard_by, window_end)
            row = await asyncio.to_thread(store.get, new_id)
            if row:
                recent.insert(0, row)
            stats["added"] += 1
            logger.info(f"🗓️ [EventMemory] 新事件 #{new_id} {ev['owner']}｜{ev['activity']}｜{ev['place']}")
    return stats

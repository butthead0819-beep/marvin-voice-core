"""
Twitch IRC 聊天收集器（匿名，不需要 Token）
用法：python scripts/twitch_collector.py [頻道名]
預設頻道：pinpinponpon627

設計原則：
- 全部訊息存下來，不預先過濾「噪音」
- 噪音 = 尚未學會的文化慣性，不是垃圾
- 意圖分類只標記，不丟棄
"""
import asyncio
import contextlib
import sqlite3
import re
import sys
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

# Single-worker executor pins all sqlite calls to one thread, so the connection
# (created on the main thread without check_same_thread=False) stays valid and
# writes stay serialized.
_DB_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="twitch-db")

CHANNEL = sys.argv[1] if len(sys.argv) > 1 else "pinpinponpon627"
DB_PATH = Path(__file__).parent.parent / "marvin_twitch.db"

IRC_HOST = "irc.chat.twitch.tv"
IRC_PORT = 6667
NICK = "justinfan88888"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Twitch] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── 普遍意圖規則（跨頻道通用，不代表全部信號）────────────────────────
# 這些是「幾乎所有實況台都適用」的硬規則
# 頻道特有的文化信號由 twitch_report.py 從資料裡學出來
INTENT_PATTERNS = {
    # 已成交事件：Twitch 自動發送的「@XXX 謝謝您的贈禮訂閱！」訊息（收禮人視角）。
    # 順序必須在 subscription_intent 之前，否則「訂閱」關鍵字會先抓走。
    # score=0 因為這不是「潛在意圖」，是「已轉化」訊號。
    "gift_received": [
        r"謝謝您的贈禮訂閱",
    ],
    "subscription_intent": [
        # 「福利」單字太弱：在此頻道也指拉霸獎品 / 活動福利，誤觸率高，已移除。
        # 「sub」單字頻道少見英文，保留。
        r"訂閱", r"\bsub\b", r"怎麼訂", r"訂了", r"剛訂", r"想訂",
        r"有什麼好處", r"訂閱.*好處", r"訂閱.*福利",
        r"訂一個月", r"\bprime\b",
    ],
    "merch_intent": [
        r"周邊", r"哪裡買", r"補貨", r"賣嗎", r"有在賣",
        r"\bmerch\b", r"商品", r"買不買得到",
    ],
    "schedule_inquiry": [
        r"下次開台", r"幾點開", r"今天有嗎", r"開台時間", r"直播時間",
        r"明天開嗎", r"schedule",
    ],
    "community_inquiry": [
        r"怎麼加入", r"身分組", r"discord", r"怎麼拿", r"member",
    ],
    # 通用問句 fallback：score=2，比 general 高、比訂閱/周邊低
    # 順序必須在 specific intents 之後，因為「想訂閱嗎？」要先被 subscription_intent 抓
    "question_intent": [
        r"[?？]$",
        r"^請問", r"^怎麼", r"^如何", r"^為什麼", r"^為何", r"^哪裡",
        r"可以.*?嗎", r"有沒有人",
    ],
}

def classify_intent(message: str) -> tuple[str, int]:
    """回傳 (intent_type, score)。general = 0 不代表無價值，代表待學習。

    特殊類別：
    - gift_received: 已成交事件（score=0），順序最前，避免被「訂閱」字 false-positive
    - subscription_intent / merch_intent: 高意圖詢問（score=3）— 潛在 lead
    - subscription_info: 訊息含「訂閱」字但語氣是公告 / 提供資訊（score=0）— 非 lead
    - question_intent: 問句 fallback（score=2）
    - 其他規則 intent: score=1

    區分 subscription_intent vs subscription_info：
      命中 subscription_intent pattern 後，若訊息是 promotional（URL / @mention 開頭 /
      「歡迎/請點/請輸入/詳情」公告語氣），降級為 subscription_info（score=0），
      不再算進熱名單或 live_intent lead。
    """
    msg_lower = message.lower()
    for intent, patterns in INTENT_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, msg_lower):
                if intent == "subscription_intent" and is_promotional_message(message):
                    return "subscription_info", 0
                if intent == "gift_received":
                    score = 0
                elif intent in ("subscription_intent", "merch_intent"):
                    score = 3
                elif intent == "question_intent":
                    score = 2
                else:
                    score = 1
                return intent, score
    return "general", 0


# ── Bot 識別 ──────────────────────────────────────────────────────────
BOT_USERNAMES = {"nightbot", "streamelements", "moobot", "fossabot", "wizebot"}


def is_bot_user(username: str) -> bool:
    """已知第三方 bot 帳號（大小寫不敏感）。"""
    return (username or "").lower() in BOT_USERNAMES


# 拉霸 / 寵物 / 機率播報等 Twitch extension 用 broadcaster 帳號發的系統訊息。
# 用前綴 emoji + 固定模板字串雙重判斷，避免一般訊息被誤殺。
_SYSTEM_BOT_PATTERNS = [
    r"^🎰\s",                    # 拉霸機 extension
    r"^🐱\s",                    # 寵物 extension
    r"目前獎池", r"今日中獎", r"目前中獎",
    r"機率｜",                   # 中獎機率公告
    r"觸發條件：",
    r"飽食度",
]
_SYSTEM_BOT_RE = re.compile("|".join(_SYSTEM_BOT_PATTERNS))


def is_system_bot_message(message: str) -> bool:
    """偵測 broadcaster 帳號被 extension 接管時發出的系統訊息。"""
    return bool(_SYSTEM_BOT_RE.search(message or ""))


# 「提供訂閱資訊」型訊息（廣告 / 工作人員回答 / @ mention 回覆）的辨識特徵。
# 用來把「我想訂閱」（lead）跟「請點 panel 訂閱」（staff/廣告）分開。
_PROMO_PATTERNS = [
    r"https?://",       # 含 URL — 一定是公告或推銷
    r"^@",              # @XXX 開頭 — 通常是回答別人
    r"^!",              # !command 開頭
    r"歡迎",            # 「歡迎 ... 訂閱」公告語
    r"請點",
    r"請輸入",
    r"詳情",
]
_PROMO_RE = re.compile("|".join(_PROMO_PATTERNS))


def is_promotional_message(message: str) -> bool:
    """偵測訊息是「提供資訊」型（廣告/staff/公告），用以區分潛在訂閱 lead。

    return True → 包含 URL、@mention 開頭、命令前綴、或「歡迎」「請點」等公告語氣
    return False → 一般陳述句（含「想訂閱」「怎麼訂」這類真實 inquiry）
    """
    return bool(_PROMO_RE.search(message or ""))


# ── SQLite 初始化 ─────────────────────────────────────────────────────
def init_db(db_path: Path) -> sqlite3.Connection:
    # check_same_thread=False because save_message runs on _DB_EXECUTOR worker,
    # not the asyncio thread. Safe because the single-worker executor serializes
    # all writes, and WAL allows the concurrent reader paths.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            channel       TEXT NOT NULL,
            username      TEXT NOT NULL,
            display_name  TEXT NOT NULL DEFAULT '',
            is_subscriber INTEGER NOT NULL DEFAULT 0,
            message       TEXT NOT NULL,
            intent_type   TEXT NOT NULL DEFAULT 'general',
            intent_score  INTEGER NOT NULL DEFAULT 0,
            session_date  TEXT NOT NULL,
            ts            TEXT NOT NULL
        )
    """)
    # 舊資料遷移：補上新欄位（ADD COLUMN 若已存在會靜默失敗）
    for col, defn in [
        ("display_name",         "TEXT NOT NULL DEFAULT ''"),
        ("is_subscriber",        "INTEGER NOT NULL DEFAULT 0"),
        # 第二波（2026-05-17）：身分 + 行為信號
        ("is_first_msg",         "INTEGER NOT NULL DEFAULT 0"),
        ("is_returning_chatter", "INTEGER NOT NULL DEFAULT 0"),
        ("sub_months",           "INTEGER NOT NULL DEFAULT 0"),
        ("is_vip",               "INTEGER NOT NULL DEFAULT 0"),
        ("is_mod",               "INTEGER NOT NULL DEFAULT 0"),
        ("is_founder",           "INTEGER NOT NULL DEFAULT 0"),
        # 第三波（2026-05-17 P0 fix）：標記 broadcaster extension bot 訊息
        ("is_system_message",    "INTEGER NOT NULL DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {defn}")
        except sqlite3.OperationalError:
            pass

    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_profiles (
            channel       TEXT NOT NULL,
            username      TEXT NOT NULL,
            display_name  TEXT NOT NULL DEFAULT '',
            is_subscriber INTEGER NOT NULL DEFAULT 0,
            updated_at    TEXT NOT NULL,
            PRIMARY KEY (channel, username)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_sessions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            channel      TEXT NOT NULL,
            username     TEXT NOT NULL,
            session_date TEXT NOT NULL,
            UNIQUE(channel, username, session_date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_user    ON messages(channel, username)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(channel, session_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_intent  ON messages(intent_type)")

    # loyalty_events：訂閱禮物 / Bits 等高價值事件（從 IRC USERNOTICE + bits tag 抓）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS loyalty_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            channel      TEXT NOT NULL,
            username     TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            event_type   TEXT NOT NULL,   -- subgift | mass_subgift | resub | sub | cheer
            amount       INTEGER NOT NULL DEFAULT 1,
            recipient    TEXT NOT NULL DEFAULT '',
            ts           TEXT NOT NULL
        )
    """)
    # tier 後加：sub-plan 1000/2000/3000 → 1/2/3
    try:
        conn.execute("ALTER TABLE loyalty_events ADD COLUMN tier INTEGER NOT NULL DEFAULT 1")
    except sqlite3.OperationalError:
        pass
    # msg_id：來自 IRC `id` tag。USERNOTICE 在 reconnect/supervisor backoff 時會
    # 重播；以 IRC id 當去重鍵可以避免雙倍計算禮物。空字串保持舊行為（不去重）。
    try:
        conn.execute("ALTER TABLE loyalty_events ADD COLUMN msg_id TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    # Partial-unique: 只對非空 msg_id 強制唯一，cheer/legacy 沒帶 id 的繼續可重複寫入。
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_loyalty_msg_id "
        "ON loyalty_events(channel, msg_id) WHERE msg_id != ''"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_loyalty_channel ON loyalty_events(channel, username)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_loyalty_ts      ON loyalty_events(ts)")

    conn.commit()
    log.info(f"DB ready: {db_path}")
    return conn


def flush_pending_commit(conn: sqlite3.Connection) -> None:
    """Commit deferred INSERTs from save_message / save_loyalty_event.

    save_message no longer commits per call. A background flusher (or test code)
    calls this on a 500ms cadence so popular streams (50+ msg/s) don't pay an
    fsync per chat line on the single-worker DB executor.
    """
    try:
        conn.commit()
    except sqlite3.Error as e:
        log.warning(f"[twitch] flush_pending_commit failed: {e}")


def save_message(
    conn: sqlite3.Connection,
    channel: str,
    username: str,
    message: str,
    display_name: str = "",
    is_subscriber: int = 0,
    is_first_msg: int = 0,
    is_returning_chatter: int = 0,
    sub_months: int = 0,
    is_vip: int = 0,
    is_mod: int = 0,
    is_founder: int = 0,
):
    intent_type, intent_score = classify_intent(message)
    # Bot 帳號的訊息再含「訂閱」「福利」等字也不該被分到高意圖類別
    if is_bot_user(username):
        intent_type, intent_score = "general", 0
    is_system_message = 1 if is_system_bot_message(message) else 0
    ts = datetime.now(timezone.utc).isoformat()
    session_date = ts[:10]
    dn = display_name or username

    conn.execute(
        """INSERT INTO messages
           (channel, username, display_name, is_subscriber,
            message, intent_type, intent_score, session_date, ts,
            is_first_msg, is_returning_chatter, sub_months,
            is_vip, is_mod, is_founder, is_system_message)
           VALUES (?,?,?,?,?,?,?,?,?, ?,?,?, ?,?,?, ?)""",
        (channel, username, dn, is_subscriber,
         message, intent_type, intent_score, session_date, ts,
         is_first_msg, is_returning_chatter, sub_months,
         is_vip, is_mod, is_founder, is_system_message),
    )
    conn.execute(
        "INSERT OR IGNORE INTO user_sessions (channel, username, session_date) VALUES (?,?,?)",
        (channel, username, session_date),
    )
    # 更新 user_profiles（upsert display_name + is_subscriber）
    conn.execute(
        """INSERT INTO user_profiles (channel, username, display_name, is_subscriber, updated_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(channel, username) DO UPDATE SET
             display_name  = excluded.display_name,
             is_subscriber = excluded.is_subscriber,
             updated_at    = excluded.updated_at""",
        (channel, username, dn, is_subscriber, ts),
    )
    # Commit is now deferred — flush_pending_commit() called by the background
    # flusher task on a 500ms cadence (or on shutdown).

    if intent_score > 0:
        log.info(f"[{intent_type}] {dn}(@{username}): {message[:60]}")


_MAX_TAG_BYTES = 8192


def parse_irc_tags(raw: str) -> dict[str, str]:
    """解析 @key=value;key=value IRC tag 字串。

    Caps the raw payload at 8 KiB and the result at 64 tags so a malformed or
    hostile line can't allocate unbounded memory before downstream sqlite writes.
    Twitch IRC tag lines are well under 4 KiB in practice.
    """
    if len(raw) > _MAX_TAG_BYTES:
        raw = raw[:_MAX_TAG_BYTES]
    out: dict[str, str] = {}
    for kv in raw.split(";"):
        if "=" not in kv:
            continue
        k, _, v = kv.partition("=")
        out[k] = v
        if len(out) >= 64:
            break
    return out


# ── 訂閱禮物 / Bits 解析 ──────────────────────────────────────────────
# msg-id → 內部 event_type 對照
USERNOTICE_EVENT_MAP = {
    "subgift":         "subgift",
    "submysterygift":  "mass_subgift",
    "resub":           "resub",
    "sub":             "sub",
}


def parse_usernotice(line: str) -> dict | None:
    """解析 IRC USERNOTICE 行，回傳 {event_type, username, display_name, amount, recipient} 或 None。

    Twitch IRC USERNOTICE 格式：
      @key=val;key=val :tmi.twitch.tv USERNOTICE #channel [:optional body]

    支援的 msg-id：subgift / submysterygift / resub / sub
    未知 msg-id 或非 USERNOTICE 行回 None。
    """
    if " USERNOTICE " not in line:
        return None
    if not line.startswith("@"):
        return None

    head, _sep, _tail = line.partition(" ")
    tags = parse_irc_tags(head[1:])
    msg_id = tags.get("msg-id", "")
    if msg_id not in USERNOTICE_EVENT_MAP:
        return None

    username = tags.get("login", "")
    display_name = tags.get("display-name", "") or username
    event_type = USERNOTICE_EVENT_MAP[msg_id]

    if event_type == "mass_subgift":
        amount = int(tags.get("msg-param-mass-gift-count", "1") or "1")
        recipient = ""
    elif event_type == "subgift":
        amount = 1
        recipient = tags.get("msg-param-recipient-user-name", "")
    else:  # resub / sub
        amount = 1
        recipient = ""

    tier = parse_sub_tier(tags.get("msg-param-sub-plan", ""))

    return {
        "event_type": event_type,
        "username": username,
        "display_name": display_name,
        "amount": amount,
        "recipient": recipient,
        "tier": tier,
        "msg_id": tags.get("id", ""),  # IRC unique id — used by save_loyalty_event for dedup
    }


def parse_badges(badges_str: str, badge_info_str: str = "") -> dict:
    """解析 IRC badges 與 badge-info tag，回傳結構化旗標。

    badges 格式：'subscriber/24,vip/1,founder/0,moderator/1,broadcaster/1'
    badge-info 格式：'subscriber/26' — 比 badges 更精準的累積月數

    回傳：{is_vip, is_mod, is_founder, is_broadcaster, sub_months}
    """
    result = {"is_vip": 0, "is_mod": 0, "is_founder": 0, "is_broadcaster": 0, "sub_months": 0}
    for source in (badges_str, badge_info_str):
        if not source:
            continue
        for badge in source.split(","):
            if "/" not in badge:
                continue
            name, val = badge.split("/", 1)
            if name == "vip":
                result["is_vip"] = 1
            elif name == "moderator":
                result["is_mod"] = 1
            elif name == "founder":
                result["is_founder"] = 1
            elif name == "broadcaster":
                result["is_broadcaster"] = 1
            elif name == "subscriber":
                try:
                    n = int(val)
                    # Twitch partner channels 可自訂 sub badge tier ID（例如 3012）。
                    # 真實累積月數理論上 < 200（16 年以上幾乎不可能），超過視為自訂 tier 不採信。
                    if n > 200:
                        continue
                    if n > result["sub_months"]:
                        result["sub_months"] = n
                except ValueError:
                    pass
    return result


SUB_TIER_MAP = {"1000": 1, "2000": 2, "3000": 3, "Prime": 1}


def parse_sub_tier(sub_plan: str) -> int:
    """msg-param-sub-plan → Tier 1/2/3，無法識別預設 1。"""
    return SUB_TIER_MAP.get(sub_plan, 1)


def parse_bits_from_tags(tags: dict[str, str]) -> int:
    """從 PRIVMSG tags 抽取 bits 數，無 / 解析失敗回 0。"""
    raw = tags.get("bits", "")
    try:
        return max(0, int(raw))
    except (ValueError, TypeError):
        return 0


def save_loyalty_event(
    conn: sqlite3.Connection,
    channel: str,
    username: str,
    display_name: str,
    event_type: str,
    amount: int = 1,
    recipient: str = "",
    tier: int = 1,
    msg_id: str = "",
):
    """Insert a loyalty event. If `msg_id` is non-empty, the partial-unique
    `idx_loyalty_msg_id` index makes the INSERT a no-op when the same IRC id
    arrives a second time (USERNOTICE replays on supervisor reconnect)."""
    ts = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT OR IGNORE INTO loyalty_events
           (channel, username, display_name, event_type, amount, recipient, ts, tier, msg_id)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (channel, username, display_name, event_type, amount, recipient, ts, tier, msg_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        log.debug(f"[loyalty] duplicate dropped msg_id={msg_id!r}")
        return
    log.info(f"[loyalty] {event_type} from {display_name}(@{username}) amount={amount} tier={tier}"
             + (f" → {recipient}" if recipient else ""))


async def _commit_flusher(conn: sqlite3.Connection, interval: float = 0.5):
    """Background coroutine: commit deferred writes every `interval` seconds.

    save_message no longer commits per call; this drains the WAL on a steady
    cadence so a crash loses ≤500ms of chat instead of paying an fsync per line.
    """
    try:
        while True:
            await asyncio.sleep(interval)
            await asyncio.get_running_loop().run_in_executor(
                _DB_EXECUTOR, flush_pending_commit, conn
            )
    except asyncio.CancelledError:
        # Final flush so shutdown doesn't leak the last batch.
        await asyncio.get_running_loop().run_in_executor(
            _DB_EXECUTOR, flush_pending_commit, conn
        )
        raise


# ── IRC 連線 ──────────────────────────────────────────────────────────
async def connect_irc(channel: str, conn: sqlite3.Connection):
    log.info(f"連線到 #{channel} ...")
    reader, writer = await asyncio.open_connection(IRC_HOST, IRC_PORT)

    def send(line: str):
        writer.write((line + "\r\n").encode())

    # 請求 tags capability → 取得 display-name, subscriber, badges 等
    send("CAP REQ :twitch.tv/tags twitch.tv/commands")
    send("PASS SCHMOOPIIE")
    send(f"NICK {NICK}")
    send(f"JOIN #{channel.lower()}")
    await writer.drain()

    # 帶 tags 的訊息格式：@tags :login!login@login.tmi.twitch.tv PRIVMSG #ch :msg
    TAGGED_RE  = re.compile(r"@([^ ]+) :(\w+)!\w+@\w+\.tmi\.twitch\.tv PRIVMSG #\w+ :(.*)")
    PLAIN_RE   = re.compile(r":(\w+)!\w+@\w+\.tmi\.twitch\.tv PRIVMSG #\w+ :(.*)")

    flusher = asyncio.create_task(_commit_flusher(conn))
    msg_count = 0
    try:
        while True:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=300)
            except asyncio.TimeoutError:
                send("PING :tmi.twitch.tv")
                await writer.drain()
                continue

            if not line:
                log.warning("連線中斷，5 秒後重連...")
                await asyncio.sleep(5)
                break

            text = line.decode("utf-8", errors="ignore").strip()

            if text.startswith("PING"):
                send("PONG :tmi.twitch.tv")
                await writer.drain()
                continue

            # USERNOTICE：訂閱禮物 / resub 等高價值事件
            if " USERNOTICE " in text:
                evt = parse_usernotice(text)
                if evt and evt["username"]:
                    await asyncio.get_running_loop().run_in_executor(
                        _DB_EXECUTOR, save_loyalty_event, conn, channel,
                        evt["username"], evt["display_name"], evt["event_type"],
                        evt["amount"], evt["recipient"], evt["tier"], evt["msg_id"],
                    )
                continue

            m = TAGGED_RE.match(text)
            if m:
                tags         = parse_irc_tags(m.group(1))
                username     = m.group(2)
                message      = m.group(3)
                display_name = tags.get("display-name", "") or username
                is_subscriber = int(tags.get("subscriber", "0"))
                bits         = parse_bits_from_tags(tags)
                is_first    = 1 if tags.get("first-msg") == "1" else 0
                is_return   = 1 if tags.get("returning-chatter") == "1" else 0
                badge_flags = parse_badges(tags.get("badges", ""), tags.get("badge-info", ""))

                await asyncio.get_running_loop().run_in_executor(
                    _DB_EXECUTOR, save_message,
                    conn, channel, username, message, display_name, is_subscriber,
                    is_first, is_return, badge_flags["sub_months"],
                    badge_flags["is_vip"], badge_flags["is_mod"], badge_flags["is_founder"],
                )
                # 含 bits 的 PRIVMSG → 額外記成 cheer loyalty event
                if bits > 0:
                    await asyncio.get_running_loop().run_in_executor(
                        _DB_EXECUTOR, save_loyalty_event, conn, channel,
                        username, display_name, "cheer", bits, "", 1,
                        tags.get("id", ""),
                    )
                msg_count += 1
                if msg_count % 100 == 0:
                    log.info(f"已收 {msg_count} 則訊息")
                continue

            # fallback：不帶 tags 的格式（不應發生但保留）
            m = PLAIN_RE.match(text)
            if m:
                await asyncio.get_running_loop().run_in_executor(
                    _DB_EXECUTOR, save_message, conn, channel, m.group(1), m.group(2),
                )
                msg_count += 1
    finally:
        flusher.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await flusher
        writer.close()


async def main():
    conn = init_db(DB_PATH)
    log.info(f"目標頻道：#{CHANNEL}  （全部訊息存下，不過濾）")
    log.info("等待開台中... Ctrl+C 停止")
    while True:
        try:
            await connect_irc(CHANNEL, conn)
        except ConnectionRefusedError:
            log.error("連線被拒，30 秒後重試")
            await asyncio.sleep(30)
        except KeyboardInterrupt:
            break
    conn.close()
    log.info("收集結束")


if __name__ == "__main__":
    asyncio.run(main())

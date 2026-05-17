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
    "subscription_intent": [
        r"訂閱", r"\bsub\b", r"怎麼訂", r"訂了", r"剛訂", r"想訂",
        r"有什麼好處", r"福利", r"訂一個月", r"prime",
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
}

def classify_intent(message: str) -> tuple[str, int]:
    """回傳 (intent_type, score)。general = 0 不代表無價值，代表待學習。"""
    msg_lower = message.lower()
    for intent, patterns in INTENT_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, msg_lower):
                score = 3 if intent in ("subscription_intent", "merch_intent") else 1
                return intent, score
    return "general", 0


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
    for col, defn in [("display_name", "TEXT NOT NULL DEFAULT ''"),
                      ("is_subscriber", "INTEGER NOT NULL DEFAULT 0")]:
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_loyalty_channel ON loyalty_events(channel, username)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_loyalty_ts      ON loyalty_events(ts)")

    conn.commit()
    log.info(f"DB ready: {db_path}")
    return conn


def save_message(
    conn: sqlite3.Connection,
    channel: str,
    username: str,
    message: str,
    display_name: str = "",
    is_subscriber: int = 0,
):
    intent_type, intent_score = classify_intent(message)
    ts = datetime.now(timezone.utc).isoformat()
    session_date = ts[:10]
    dn = display_name or username

    conn.execute(
        """INSERT INTO messages
           (channel, username, display_name, is_subscriber,
            message, intent_type, intent_score, session_date, ts)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (channel, username, dn, is_subscriber,
         message, intent_type, intent_score, session_date, ts),
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
    conn.commit()

    if intent_score > 0:
        log.info(f"[{intent_type}] {dn}(@{username}): {message[:60]}")


def parse_irc_tags(raw: str) -> dict[str, str]:
    """解析 @key=value;key=value IRC tag 字串。"""
    return dict(kv.split("=", 1) for kv in raw.split(";") if "=" in kv)


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

    return {
        "event_type": event_type,
        "username": username,
        "display_name": display_name,
        "amount": amount,
        "recipient": recipient,
    }


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
):
    ts = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO loyalty_events
           (channel, username, display_name, event_type, amount, recipient, ts)
           VALUES (?,?,?,?,?,?,?)""",
        (channel, username, display_name, event_type, amount, recipient, ts),
    )
    conn.commit()
    log.info(f"[loyalty] {event_type} from {display_name}(@{username}) amount={amount}"
             + (f" → {recipient}" if recipient else ""))


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

    msg_count = 0
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
                    evt["amount"], evt["recipient"],
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
            # sqlite3 calls are sync — push to threadpool so the IRC loop
            # doesn't stall under chat bursts (popular channels can spike 50+ msg/s).
            await asyncio.get_running_loop().run_in_executor(
                _DB_EXECUTOR, save_message, conn, channel, username, message, display_name, is_subscriber,
            )
            # 含 bits 的 PRIVMSG → 額外記成 cheer loyalty event
            if bits > 0:
                await asyncio.get_running_loop().run_in_executor(
                    _DB_EXECUTOR, save_loyalty_event, conn, channel,
                    username, display_name, "cheer", bits, "",
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

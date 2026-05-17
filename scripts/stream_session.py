"""
開台 session 管理器：收資料 → 自動產報表
用法：python scripts/stream_session.py [頻道名] [時數]
預設：pinpinponpon627，6 小時

由 cron 自動呼叫，開台前 10 分鐘啟動。
"""
import asyncio
import sqlite3
import re
import sys
import logging
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from twitch_collector import init_db, save_message, IRC_HOST, IRC_PORT, NICK  # noqa: E402
from twitch_report import run_report                                            # noqa: E402
from gen_pitch_html import generate as gen_html                                 # noqa: E402

DB_PATH = Path(__file__).parent.parent / "marvin_twitch.db"
LOG_DIR = Path(__file__).parent.parent / "logs"
log = logging.getLogger(__name__)


def _setup_logging(channel: str):
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"session_{channel}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [Session] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    return log_file


async def collect_with_timeout(channel: str, conn: sqlite3.Connection, duration_sec: float):
    """收資料直到 duration_sec 秒後自動停止"""
    async def _connect():
        while True:
            try:
                log.info(f"連線到 #{channel} ...")
                reader, writer = await asyncio.open_connection(IRC_HOST, IRC_PORT)

                def send(line):
                    writer.write((line + "\r\n").encode())

                send("CAP REQ :twitch.tv/tags twitch.tv/commands")
                send("PASS SCHMOOPIIE")
                send(f"NICK {NICK}")
                send(f"JOIN #{channel.lower()}")
                await writer.drain()

                TAGGED_RE = re.compile(
                    r"@([^ ]+) :(\w+)!\w+@\w+\.tmi\.twitch\.tv PRIVMSG #\w+ :(.*)"
                )
                PLAIN_RE  = re.compile(
                    r":(\w+)!\w+@\w+\.tmi\.twitch\.tv PRIVMSG #\w+ :(.*)"
                )

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

                    m = TAGGED_RE.match(text)
                    if m:
                        tags = dict(
                            kv.split("=", 1) for kv in m.group(1).split(";") if "=" in kv
                        )
                        username     = m.group(2)
                        message      = m.group(3)
                        display_name = tags.get("display-name", "") or username
                        is_subscriber = int(tags.get("subscriber", "0"))
                        save_message(conn, channel, username, message, display_name, is_subscriber)
                        msg_count += 1
                        if msg_count % 100 == 0:
                            log.info(f"已收 {msg_count} 則訊息")
                        continue

                    m = PLAIN_RE.match(text)
                    if m:
                        save_message(conn, channel, m.group(1), m.group(2))
                        msg_count += 1

                writer.close()

            except ConnectionRefusedError:
                log.error("連線被拒，30 秒後重試")
                await asyncio.sleep(30)

    try:
        await asyncio.wait_for(_connect(), timeout=duration_sec)
    except asyncio.TimeoutError:
        log.info(f"Session 到時（{duration_sec/3600:.1f}h），停止收集")


async def start_stt_listener(channel: str) -> asyncio.subprocess.Process:
    """啟動 twitch_stt_listener.py 子行程，回傳 process 物件。"""
    script = Path(__file__).parent / "twitch_stt_listener.py"
    python = Path(__file__).parent.parent / "venv_simon" / "bin" / "python3"
    proc = await asyncio.create_subprocess_exec(
        str(python), str(script), channel,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    log.info(f"[STT Listener] 已啟動 (pid={proc.pid})")
    return proc


async def run_with_stt(channel: str, duration_hours: float):
    """收聊天室 + 收語音同時跑，session 結束後一起停。"""
    conn = init_db(DB_PATH)
    stt_proc = await start_stt_listener(channel)

    try:
        await collect_with_timeout(channel, conn, duration_hours * 3600)
    finally:
        conn.close()
        stt_proc.terminate()
        try:
            await asyncio.wait_for(stt_proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            stt_proc.kill()
        log.info("[STT Listener] 已停止")


async def main():
    channel        = sys.argv[1] if len(sys.argv) > 1 else "pinpinponpon627"
    duration_hours = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0
    log_file       = _setup_logging(channel)

    log.info(f"=== Session 開始：#{channel}，預計 {duration_hours}h ===")
    await run_with_stt(channel, duration_hours)

    log.info("產生報表中...")
    run_report(channel, top_n=10, days=1)
    run_report(channel, top_n=10, days=30)

    log.info("產生 pitch demo HTML...")
    out = gen_html(channel, days=1)
    if out:
        log.info(f"pitch demo → {out}")

    log.info(f"=== Session 結束，log: {log_file} ===")


if __name__ == "__main__":
    asyncio.run(main())

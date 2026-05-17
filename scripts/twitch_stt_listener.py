"""
Twitch 直播語音 → macOS STT → Groq Whisper（fallback）→ 上下文摘要
用法：python scripts/twitch_stt_listener.py [頻道名]

流程：
  yt-dlp 抓 Twitch HLS 音訊串流
  → ffmpeg 切成 12 秒 WAV 片段
  → _run_macos_stt  主引擎（Apple SFSpeechRecognizer）
  → _run_groq_stt   備援（Groq whisper-large-v3-turbo，28800s/day）
  → 輸出時間戳記文字，寫入 DB

注意：Apple SFSpeechRecognizer 有未公開速率限制，
      連續超過 1 小時可能被節流，此時自動 fallback 到 Groq。
"""
import asyncio
import contextlib
import subprocess
import sqlite3
import sys
import os
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PROJ_ROOT      = Path(__file__).parent.parent
STT_BIN        = PROJ_ROOT / "macos_stt_bin"
DB_PATH        = PROJ_ROOT / "marvin_twitch.db"
CHUNK_SECS     = 12
OVERLAP_SECS   = 1
MAX_TRANSCRIPT = 40

log = logging.getLogger(__name__)


def _setup_logging(channel: str):
    log_dir = PROJ_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [TwitchSTT] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(log_dir / f"twitch_stt_{channel}.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


# ── DB ───────────────────────────────────────────────────────────────
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stream_transcript (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            channel  TEXT NOT NULL,
            text     TEXT NOT NULL,
            ts       TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tr_channel ON stream_transcript(channel, ts)")
    conn.commit()
    return conn

def save_transcript(conn: sqlite3.Connection, channel: str, text: str):
    ts = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO stream_transcript (channel, text, ts) VALUES (?,?,?)",
        (channel, text, ts)
    )
    conn.execute("""
        DELETE FROM stream_transcript
        WHERE channel = ? AND id NOT IN (
            SELECT id FROM stream_transcript
            WHERE channel = ?
            ORDER BY id DESC LIMIT ?
        )
    """, (channel, channel, MAX_TRANSCRIPT))
    conn.commit()

def get_recent_context(conn: sqlite3.Connection, channel: str, n: int = 5) -> str:
    rows = conn.execute(
        "SELECT text FROM stream_transcript WHERE channel = ? ORDER BY id DESC LIMIT ?",
        (channel, n)
    ).fetchall()
    return "｜".join(r[0] for r in reversed(rows))


# ── STT 引擎（可獨立測試）────────────────────────────────────────────

def _run_macos_stt(wav_path: Path) -> str | None:
    """呼叫 macos_stt_bin，回傳文字或 None。"""
    env = os.environ.copy()
    env["STT_CONTEXT_STRINGS"] = (
        "蘋蘋澎澎,旅團,戰艦世界,碧藍航線,訂閱,直播,開台,"
        "咪那桑,聯播,蘋潔,代碼,AZURLANEWAVE8"
    )
    try:
        result = subprocess.run(
            [str(STT_BIN), str(wav_path)],
            capture_output=True, text=True,
            timeout=20, env=env
        )
        lines = [l.strip() for l in result.stdout.splitlines()
                 if l.strip() and not l.startswith(("🔍", "✅", "❌", "📚"))]
        return lines[-1] if lines else None
    except subprocess.TimeoutExpired:
        log.warning(f"macOS STT timeout: {wav_path.name}")
        return None
    except Exception as e:
        log.error(f"macOS STT error: {e}")
        return None


def _run_groq_stt(wav_path: Path) -> str | None:
    """Groq whisper-large-v3-turbo 備援，回傳文字或 None。"""
    groq_key = os.getenv("GROQ_API_KEY", "")
    if not groq_key:
        return None
    try:
        from groq import Groq
        client = Groq(api_key=groq_key)
        with open(wav_path, "rb") as f:
            resp = client.audio.transcriptions.create(
                model="whisper-large-v3-turbo",
                file=("audio.wav", f, "audio/wav"),
                language="zh",
                prompt="蘋蘋澎澎,旅團,戰艦世界,碧藍航線,訂閱,直播,開台,咪那桑",
            )
        text = resp.text.strip()
        return text if text else None
    except Exception as e:
        log.warning(f"Groq STT error: {e}")
        return None


def run_stt(wav_path: Path) -> str | None:
    """主 STT 入口：macOS 優先，無結果時 fallback 到 Groq（key 檢查在 _run_groq_stt 內）。"""
    text = _run_macos_stt(wav_path)
    if text:
        return text
    log.info(f"[STT] macOS 無結果，fallback → Groq ({wav_path.name})")
    return _run_groq_stt(wav_path)


# ── ffmpeg 片段生成 ───────────────────────────────────────────────────
async def segment_stream(stream_url: str, chunk_dir: Path, chunk_secs: int):
    cmd = [
        "ffmpeg", "-loglevel", "error",
        "-i", stream_url,
        "-vn",
        "-ar", "16000",
        "-ac", "1",
        "-f", "segment",
        "-segment_time", str(chunk_secs),
        "-segment_format", "wav",
        "-reset_timestamps", "1",
        str(chunk_dir / "chunk_%03d.wav"),
    ]
    log.info(f"ffmpeg 開始切片：{chunk_secs}s 片段")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    return proc


async def get_stream_url(channel: str) -> str | None:
    ytdlp = str(PROJ_ROOT / "venv_simon" / "bin" / "yt-dlp")
    result = await asyncio.create_subprocess_exec(
        ytdlp, "--get-url", "-f", "bestaudio",
        f"https://www.twitch.tv/{channel}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await result.communicate()
    url = stdout.decode().strip().split("\n")[0]
    if not url or "Error" in stderr.decode():
        return None
    return url


async def wait_for_stream(channel: str, poll_interval: int = 60) -> str:
    """輪詢直到頻道開台，回傳 HLS URL。每 poll_interval 秒重試一次。"""
    attempt = 0
    while True:
        url = await get_stream_url(channel)
        if url:
            if attempt > 0:
                log.info(f"[STT] #{channel} 開台了！取得串流 URL")
            return url
        attempt += 1
        log.info(f"[STT] #{channel} 未開台，{poll_interval}s 後重試（第 {attempt} 次）")
        await asyncio.sleep(poll_interval)


# ── 主循環 ───────────────────────────────────────────────────────────
async def process_chunks(chunk_dir: Path, conn: sqlite3.Connection, channel: str):
    seen: set[str] = set()
    consecutive_empty = 0

    while True:
        await asyncio.sleep(2)
        wavs = sorted(chunk_dir.glob("chunk_*.wav"))
        complete = wavs[:-1] if len(wavs) > 1 else []

        for wav in complete:
            if wav.name in seen:
                continue
            seen.add(wav.name)

            size = wav.stat().st_size
            if size < 16000 * 2:
                wav.unlink(missing_ok=True)
                continue

            text = await asyncio.to_thread(run_stt, wav)
            wav.unlink(missing_ok=True)

            if not text:
                consecutive_empty += 1
                if consecutive_empty >= 5:
                    log.warning("連續 5 段無文字，可能靜音或 STT 節流")
                continue

            consecutive_empty = 0
            save_transcript(conn, channel, text)
            context = get_recent_context(conn, channel)
            log.info(f"[transcript] {text}")
            log.info(f"[context]    {context[:80]}...")


async def _drain_ffmpeg_stderr(proc: asyncio.subprocess.Process):
    """Drain ffmpeg stderr so it doesn't block, and surface errors to log."""
    if proc.stderr is None:
        return
    try:
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            msg = line.decode(errors="ignore").strip()
            if msg:
                log.warning(f"[ffmpeg] {msg}")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning(f"[ffmpeg] stderr drain error: {e}")


async def run_listener(channel: str):
    """Supervisor 迴圈：ffmpeg 死掉就重抓 stream URL 重啟。

    Twitch HLS URL 約 30-60 分鐘會過期；舊版用 asyncio.gather 包死的
    ffmpeg.communicate() 與 process_chunks() 不會互相 cancel，導致 ffmpeg
    死後 process_chunks 變空跑且整體看起來「卡住但沒錯」。改成 supervisor
    迴圈後，ffmpeg 結束 → 取消 processor → 重抓 URL 重來，並 backoff 避免
    Twitch 暫斷時的緊密重試。
    """
    log.info(f"=== Twitch STT 啟動：#{channel} ===")
    conn = init_db()

    BACKOFF_BASE = 5
    BACKOFF_MAX  = 60
    backoff = BACKOFF_BASE

    try:
        while True:
            url = await wait_for_stream(channel)
            log.info("串流 URL 取得，開始切片...")

            with tempfile.TemporaryDirectory(prefix="twitch_stt_") as tmp:
                chunk_dir = Path(tmp)
                ffmpeg_proc = await segment_stream(url, chunk_dir, CHUNK_SECS)

                processor_task = asyncio.create_task(
                    process_chunks(chunk_dir, conn, channel)
                )
                stderr_task = asyncio.create_task(_drain_ffmpeg_stderr(ffmpeg_proc))

                rc = await ffmpeg_proc.wait()
                log.warning(
                    f"ffmpeg 結束 returncode={rc}，stream URL 可能過期或網路中斷，"
                    f"{backoff}s 後重抓 URL 重來"
                )

                processor_task.cancel()
                stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await processor_task
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await stderr_task

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX) if rc != 0 else BACKOFF_BASE
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        conn.close()
        log.info("=== Twitch STT 結束 ===")


async def main():
    channel = sys.argv[1] if len(sys.argv) > 1 else "pinpinponpon627"
    _setup_logging(channel)
    await run_listener(channel)


if __name__ == "__main__":
    asyncio.run(main())

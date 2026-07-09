"""把剛結束場次的當夜策展「今夜歌單」畫成一格貼回 #馬文的厭世日記（接 slow_system_loop 靜默觸發）。

2026-07 縮減自舊的多格 AI 生圖漫畫（使用者拍板「一天只出一格，內容就是策展」）——
日記唯一內容 = 策展卡（只下 YouTube 縮圖、零 AI 生圖、零付費）。本場無策展歌單 → 不出日記。
全防禦：任何失敗都吞掉、絕不影響 slow loop。觸發時機 = 靜默（場次收尾）；同一場次靠 state 檔去重。
"""
import asyncio
import io
import json
import logging
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

LOG_PATH = "records/chat_summary_log.txt"
DB_PATH = "marvin.db"
STATE_PATH = "records/diary_comic_last.json"
PENDING_PATH = "records/diary_comic_pending.json"
DIARY_CHANNELS = ("馬文的厭世日記", "marvin-diary")
# （保留給 plan_latest_session 測試）出漫畫門檻：≥N 段（每段 ~10 分鐘對話）才值得燒生圖。6→10 約省 33% 生圖成本
# （只出夠熱鬧的場次＝品質正篩，安靜場次本來也沒料可畫）。可調此值權衡頻率 vs 成本。
DIARY_MIN_ENTRIES = 6   # 2026-07-03 10→6（使用者拍板）：10 擋掉上週 5/6 晚，短場也值一格 meme（~$0.04）


def _last_posted() -> str:
    try:
        return json.loads(Path(STATE_PATH).read_text(encoding="utf-8")).get("end", "")
    except Exception:
        return ""


def _mark_posted(end: str) -> None:
    try:
        Path(STATE_PATH).write_text(json.dumps({"end": end}), encoding="utf-8")
    except Exception:
        pass


def _pending() -> dict:
    """已渲染、待下次開台才貼的那頁（end/path/format）。無→{}。"""
    try:
        return json.loads(Path(PENDING_PATH).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _set_pending(end: str, path: str, fmt: str) -> None:
    try:
        Path(PENDING_PATH).write_text(
            json.dumps({"end": end, "path": path, "format": fmt}), encoding="utf-8")
    except Exception:
        pass


def _clear_pending() -> None:
    try:
        Path(PENDING_PATH).unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _db_rows(start_ts_str: str, end_ts_str: str, db_path: str = DB_PATH):
    """從 marvin.db 撈場次時間窗（前後各留 10 分鐘）的逐字稿 (speaker,text,ts)。

    給 find_highlights 找爆笑點用。任何失敗（壞時戳/無 DB/無表）→ []，不炸 loop。
    """
    import datetime
    import sqlite3
    try:
        lo = datetime.datetime.fromisoformat(start_ts_str).timestamp() - 600
        hi = datetime.datetime.fromisoformat(end_ts_str).timestamp() + 600
    except (ValueError, TypeError):
        return []
    try:
        con = sqlite3.connect(db_path)
        try:
            return con.execute(
                "SELECT speaker, text, timestamp FROM transcripts "
                "WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
                (lo, hi)).fetchall()
        finally:
            con.close()
    except Exception:
        return []


def plan_latest_session(log_text: str, rows_fn):
    """日誌文字 + (start,end)->原始逐字稿函式 → (session, StoryPlan, end) 或 None。

    策展（curate）選 Hero：搶話峰值（≥在場×ratio）→ 不夠熱退最強話題。有料(≥6段)就出：
    crosstalk→slant 整頁、topic→meme 單格。<6 段或無場次 → None。
    """
    import sys
    sys.path.insert(0, ".")
    from diary_comic.parser import (
        parse_log, dedupe_adjacent, eligible_sessions, should_generate)
    from diary_comic.curator import curate
    from diary_comic.curation_render import curation_to_story_plan

    sessions = eligible_sessions(dedupe_adjacent(parse_log(log_text)))
    if not sessions:
        logger.info("📓 [DiaryComic] skip: 無可用場次")
        return None
    session = sessions[-1]
    if not should_generate(session, min_entries=DIARY_MIN_ENTRIES):
        # 2026-07-03 補觀測：6/26-7/1 五晚短場全被此閘靜默跳過，使用者以為管線壞了
        logger.info(f"📓 [DiaryComic] skip: 場次僅 {len(session)} 段 < {DIARY_MIN_ENTRIES}（太短不燒圖錢）")
        return None  # 內容不足 DIARY_MIN_ENTRIES 段（對話太短）→ 不值得燒生圖錢
    end = session[-1].ts_str
    cur = curate(rows_fn(session[0].ts_str, end), session)
    if cur is None:
        logger.info("📓 [DiaryComic] skip: curator 回 None（無可策展內容）")
        return None
    return session, curation_to_story_plan(cur), end


_QUOTE_SYS = (
    "你是馬文——《銀河便車指南》那個厭世、存在主義、聰明絕頂卻無比厭倦的機器人。"
    "看今晚這群人聊的主題，寫一句你對這一切的厭世吐槽，當日記開頁語錄。"
    "繁中、≤28 字、毒舌帶哲思、像在嘆氣。只回那句話，不要引號。")


def _gen_marvin_quote(session, text_fn) -> str:
    """現場生今夜馬文語錄（碎念欄已停產 → 用 LLM 從當夜主題生）。失敗→空。"""
    if text_fn is None or not session:
        return ""
    try:
        topics = "、".join(e.core for e in session[:12] if e.core)
        if not topics:
            return ""
        q = (text_fn(_QUOTE_SYS, f"今晚他們聊了：{topics}\n\n你的厭世語錄：") or "").strip()
        return q.strip("「」\"'　 ")[:40]
    except Exception:
        return ""


def _render_themed_card(session):
    """當夜 Marvin 策展的「今夜歌單」獨立卡（含 cover art）＝縮減後日記的唯一內容。

    資料源異於點歌台（那是使用者主動點歌的 bot log）——這是 Marvin 自策展、附選歌理由。
    一晚多張取最後一張（latest_themed_set）。零 AI 生圖（只下 YouTube 縮圖）。
    無策展歌單 / 任何失敗 → None（caller 據此決定不出日記）。
    """
    try:
        import datetime as _dt
        import io
        import urllib.request
        from PIL import Image
        from diary_comic.themed_set import latest_themed_set
        from diary_comic.song_requests import video_id_from_url, thumb_url
        from diary_comic.layout import compose_themed_set_card
        from themed_playlist import _THEMED_SET_LOG  # 單一事實來源：writer 同一路徑

        since = _dt.datetime.fromisoformat(session[0].ts_str).timestamp() - 600
        until = _dt.datetime.fromisoformat(session[-1].ts_str).timestamp() + 600
        rec = latest_themed_set(
            Path(_THEMED_SET_LOG).read_text(encoding="utf-8", errors="ignore"), since, until)
        if rec is None or not rec.picks:
            return None
        covers = []
        for p in rec.picks[:8]:
            img = None
            vid = video_id_from_url(p.get("url") or "")
            if vid:
                try:
                    with urllib.request.urlopen(thumb_url(vid), timeout=10) as r:
                        img = Image.open(io.BytesIO(r.read())).convert("RGB")
                except Exception:
                    pass
            covers.append(img)
        return compose_themed_set_card(rec.theme_title, rec.picks, covers=covers)
    except Exception as e:
        logger.debug(f"[DiaryComic] 今夜歌單卡略過: {e}")
        return None


def _latest_session():
    """拿最近一場的 session（給時間窗 + end 去重）。無 curator、無 min_entries 閘——
    縮減後日記由「有沒有策展歌單」決定，不由對話熱鬧度決定。無場次 → None。"""
    try:
        import sys
        sys.path.insert(0, ".")
        from diary_comic.parser import parse_log, dedupe_adjacent, eligible_sessions
        sessions = eligible_sessions(dedupe_adjacent(
            parse_log(Path(LOG_PATH).read_text(encoding="utf-8", errors="ignore"))))
        return sessions[-1] if sessions else None
    except Exception as e:
        logger.debug(f"[DiaryComic] 取場次失敗: {e}")
        return None


def _render_blocking():
    """同步：選剛結束場次 → 只渲染當夜策展「今夜歌單」一格 → 存檔 + 標 pending（等下次開台才貼）。

    縮減自舊的多格 AI 生圖漫畫（2026-07：使用者拍板「一天只出一格，內容就是策展」）——
    日記唯一內容 = 策展卡（零 AI 生圖、零付費）。本場無策展歌單 → 不出日記。
    回 (png, "themed") 或 None。在 to_thread 跑。已貼過 / 已渲染待貼的同場次 → 跳過。
    """
    session = _latest_session()
    if not session:
        return None
    end = session[-1].ts_str
    if end == _last_posted() or end == _pending().get("end"):
        logger.info(f"📓 [DiaryComic] skip: 場次 {end} 已貼過/已渲染待貼")
        return None  # 已貼過、或已渲染待貼

    card = _render_themed_card(session)  # 當夜策展卡；無策展 → None
    if card is None:
        logger.info(f"📓 [DiaryComic] skip: 場次 {end} 本場無策展歌單，不出日記")
        return None
    out = f"records/diary_comic_{end.replace(':', '').replace(' ', '_').replace('-', '')}.png"
    card.save(out)
    _set_pending(end, out, "themed")  # 等下次開台才貼
    return out, "themed"


def _find_diary_channel(bot):
    import discord
    for guild in getattr(bot, "guilds", []) or []:
        for name in DIARY_CHANNELS:
            ch = discord.utils.get(guild.text_channels, name=name)
            if ch:
                return ch
    return None


async def maybe_render_diary(bot, active_text_channel=None):
    """關台（靜默）時呼叫：渲染當夜策展卡、標 pending，**不貼**。任何失敗都吞掉。"""
    try:
        await asyncio.to_thread(_render_blocking)
    except Exception as e:
        logger.warning(f"⚠️ [DiaryComic] 渲染失敗（已吞，不影響 loop）: {e}")


async def maybe_post_diary(bot):
    """開台（有人進語音）時呼叫：把 pending 那頁貼回日記頻道 + 置頂。

    回 (channel, format) 供呼叫端語音預告；無 pending / 失敗 → None。
    """
    try:
        p = _pending()
        end, path = p.get("end"), p.get("path")
        if not end or not path or end == _last_posted():
            return None
        import discord
        from pathlib import Path as _P
        if not _P(path).exists():
            _clear_pending()
            return None
        target = _find_diary_channel(bot)
        if target is None:
            return None
        msg = await target.send(content="📓 馬文的昨日日記", file=discord.File(path))
        try:
            await msg.pin()  # 置頂 → 晚進來的人不用爬
        except Exception:
            pass
        _mark_posted(end)
        _clear_pending()
        logger.info(f"📓 [DiaryComic] 已貼昨日漫畫 {path}（{p.get('format')}）並置頂")
        return target, p.get("format", "")
    except Exception as e:
        logger.warning(f"⚠️ [DiaryComic] 貼漫畫失敗（已吞）: {e}")
        return None


async def maybe_post_open_rituals(bot):
    """開台儀式總入口：貼昨日日記 + 昨夜回放秀（兩者各自 idempotent、全防禦）。

    集中在這（非 voice_controller）以免 god-object 漲行。回 diary 的 posted 供語音預告判斷。
    """
    posted = await maybe_post_diary(bot)
    try:
        from make_reveal import maybe_post_reveal
        await maybe_post_reveal(bot)
    except Exception as e:
        logger.debug(f"[Reveal] 開台發布略過（已吞）: {e}")
    return posted

"""把剛結束的對話場次畫成漫畫貼回 #馬文的厭世日記（接 slow_system_loop 靜默觸發）。

全防禦：任何失敗都吞掉、絕不影響 slow loop。出圖走 PaidUsageGuard 入帳 + daily/monthly cap。
觸發時機 = 靜默（場次收尾）；同一場次靠 state 檔去重，不重出。
"""
import asyncio
import base64
import io
import json
import logging
import os
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

LOG_PATH = "records/chat_summary_log.txt"
DB_PATH = "marvin.db"
STATE_PATH = "records/diary_comic_last.json"
PENDING_PATH = "records/diary_comic_pending.json"
CACHE_DIR = "records/diary_comic_cache"
BOT_LOG = os.path.expanduser("~/Library/Logs/Marvin/bot_stdout.log")  # [點歌-手動] 來源
MUSIC_MEMORY = "music_memory.json"  # 歌名→video id→cover 縮圖
DIARY_CHANNELS = ("馬文的厭世日記", "marvin-diary")
IMG_MODEL = "gemini-2.5-flash-image"
TEXT_MODEL = "gemini-2.5-flash"
EST_USD_PER_IMG = 0.04
# 出漫畫門檻：≥N 段（每段 ~10 分鐘對話）才值得燒生圖。6→10 約省 33% 生圖成本
# （只出夠熱鬧的場次＝品質正篩，安靜場次本來也沒料可畫）。可調此值權衡頻率 vs 成本。
DIARY_MIN_ENTRIES = 10


def _key() -> str:
    k = os.environ.get("GEMINI_PAID_API_KEY", "")
    if k:
        return k
    try:
        for line in open(".env", encoding="utf-8"):
            if line.strip().startswith("GEMINI_PAID_API_KEY="):
                return line.strip().split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return ""


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


def _img_fn(key, guard):
    def gen(prompt, aspect=None):
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{IMG_MODEL}:generateContent?key={key}")
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        if aspect:
            payload["generationConfig"] = {"imageConfig": {"aspectRatio": aspect}}
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read())
        from PIL import Image
        for p in d["candidates"][0]["content"]["parts"]:
            inl = p.get("inlineData") or p.get("inline_data")
            if inl and inl.get("data"):
                img = Image.open(io.BytesIO(base64.b64decode(inl["data"]))).convert("RGB")
                if guard is not None:
                    guard.record(caller="diary_comic", model=IMG_MODEL,
                                 tokens=1290, est_usd=EST_USD_PER_IMG)  # 入帳
                return img
        raise RuntimeError("no image in response")
    return gen


def _text_fn(key):
    def gen(system, user):
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{TEXT_MODEL}:generateContent?key={key}")
        body = json.dumps({
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"parts": [{"text": user}]}],
        }).encode()
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
        return d["candidates"][0]["content"]["parts"][0]["text"]
    return gen


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
        return None
    session = sessions[-1]
    if not should_generate(session, min_entries=DIARY_MIN_ENTRIES):
        return None  # 內容不足 DIARY_MIN_ENTRIES 段（對話太短）→ 不值得燒生圖錢
    end = session[-1].ts_str
    cur = curate(rows_fn(session[0].ts_str, end), session)
    if cur is None:
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


def _prepend_marvin_quote(page, session, text_fn=None):
    """把今夜馬文語錄接在頁上方當 epigraph。全防禦→失敗回原圖。"""
    try:
        from diary_comic.layout import prepend_quote
        return prepend_quote(page, _gen_marvin_quote(session, text_fn))
    except Exception as e:
        logger.debug(f"[DiaryComic] 馬文語錄略過: {e}")
        return page


def _append_song_card(page, session):
    """關台時把當夜使用者主動點歌畫成「點歌台」一格接在頁下方（含 cover art）。全防禦→失敗回原圖。"""
    try:
        import datetime as _dt
        import io
        import json as _json
        import urllib.request
        from PIL import Image
        from diary_comic.song_requests import parse_manual_requests, build_title_index, thumb_url
        from diary_comic.layout import append_song_card

        since = _dt.datetime.fromisoformat(session[0].ts_str).timestamp() - 600
        until = _dt.datetime.fromisoformat(session[-1].ts_str).timestamp() + 600
        reqs = parse_manual_requests(
            Path(BOT_LOG).read_text(encoding="utf-8", errors="ignore"), since, until)
        if not reqs:
            return page
        try:
            idx = build_title_index(
                _json.loads(Path(MUSIC_MEMORY).read_text(encoding="utf-8")).get("songs", {}))
        except Exception:
            idx = {}
        covers = []
        for _u, title in reqs[:8]:
            img = None
            vid = idx.get(title)
            if vid:
                try:
                    with urllib.request.urlopen(thumb_url(vid), timeout=10) as r:
                        img = Image.open(io.BytesIO(r.read())).convert("RGB")
                except Exception:
                    pass
            covers.append(img)
        return append_song_card(page, reqs, covers)
    except Exception as e:
        logger.debug(f"[DiaryComic] 點歌台略過: {e}")
        return page


def _render_blocking(key: str):
    """同步：選剛結束場次 → 策展排版 → 出圖 + 點歌台 → 存檔 + 標 pending（不貼，等下次開台）。

    回 (png, format) 或 None。在 to_thread 跑。已貼過 / 已渲染待貼的同場次 → 跳過。
    """
    import datetime
    import sys
    sys.path.insert(0, ".")
    from diary_comic.render import render_story
    try:
        from llm_paid import PaidUsageGuard
        guard = PaidUsageGuard()
    except Exception:
        guard = None

    planned = plan_latest_session(
        Path(LOG_PATH).read_text(encoding="utf-8"), _db_rows)
    if not planned:
        return None
    session, plan, end = planned
    if end == _last_posted() or end == _pending().get("end"):
        return None  # 已貼過、或已渲染待貼

    npanels = 1 if plan.format == "meme" else 4  # meme 單格 / slant 四格
    if guard is not None and not guard.allow(EST_USD_PER_IMG * npanels):
        logger.warning("💰 [DiaryComic] 超 spending cap，今天不出漫畫")
        return None

    page = render_story(
        plan, img_fn=_img_fn(key, guard), text_fn=_text_fn(key),
        cache_dir=CACHE_DIR, page_size=(1080, 1920), variant="nano",
        day_index=datetime.date.today().toordinal())
    if page is None:
        return None
    page = _append_song_card(page, session)  # 接「今夜點歌台」一格（有點歌才接，全防禦）
    page = _prepend_marvin_quote(page, session, _text_fn(key))  # 開頁接馬文語錄（LLM 生，全防禦）
    out = f"records/diary_comic_{end.replace(':', '').replace(' ', '_').replace('-', '')}.png"
    page.save(out)
    _set_pending(end, out, plan.format)  # 等下次開台才貼
    return out, plan.format


def _find_diary_channel(bot):
    import discord
    for guild in getattr(bot, "guilds", []) or []:
        for name in DIARY_CHANNELS:
            ch = discord.utils.get(guild.text_channels, name=name)
            if ch:
                return ch
    return None


async def maybe_render_diary(bot, active_text_channel=None):
    """關台（靜默）時呼叫：策展出圖、標 pending，**不貼**。任何失敗都吞掉。"""
    try:
        key = _key()
        if not key:
            return
        await asyncio.to_thread(_render_blocking, key)
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

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
STATE_PATH = "records/diary_comic_last.json"
CACHE_DIR = "records/diary_comic_cache"
DIARY_CHANNELS = ("馬文的厭世日記", "marvin-diary")
IMG_MODEL = "gemini-2.5-flash-image"
TEXT_MODEL = "gemini-2.5-flash"
EST_USD_PER_IMG = 0.04


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


def _render_blocking(key: str):
    """同步：選剛結束場次 → 出圖 → 存檔。回 (png, layout, line) 或 None。在 to_thread 跑。"""
    import sys
    sys.path.insert(0, ".")
    from diary_comic.parser import (
        parse_log, dedupe_adjacent, eligible_sessions, choose_style, should_generate)
    from diary_comic.render import render_session
    try:
        from llm_paid import PaidUsageGuard
        guard = PaidUsageGuard()
    except Exception:
        guard = None

    sessions = eligible_sessions(dedupe_adjacent(parse_log(
        Path(LOG_PATH).read_text(encoding="utf-8"))))
    if not sessions:
        return None
    session = sessions[-1]
    if not should_generate(session, min_entries=6):
        return None  # 內容不足 6 筆（對話太短）→ 不值得燒 API 出漫畫
    end = session[-1].ts_str
    if end == _last_posted():
        return None  # 已出過這場次

    layout = choose_style(session)
    npanels = min(8, len(session)) if layout == "webtoon" else min(4, len(session))
    if guard is not None and not guard.allow(EST_USD_PER_IMG * npanels):
        logger.warning("💰 [DiaryComic] 超 spending cap，今天不出漫畫")
        return None

    page, used, line = render_session(
        session, img_fn=_img_fn(key, guard), text_fn=_text_fn(key),
        cache_dir=CACHE_DIR, page_size=(1080, 1920), variant="nano")
    out = f"records/diary_comic_{end.replace(':', '').replace(' ', '_').replace('-', '')}.png"
    page.save(out)
    _mark_posted(end)
    return out, used, line


async def maybe_post_comic(bot, active_text_channel):
    """靜默時呼叫：把剛結束的場次畫成漫畫貼回日記頻道。任何失敗都吞掉。"""
    try:
        key = _key()
        if not key:
            return
        result = await asyncio.to_thread(_render_blocking, key)
        if not result:
            return
        out, used, line = result
        import discord
        target = None
        guild = active_text_channel.guild if active_text_channel else None
        if guild:
            for name in DIARY_CHANNELS:
                target = discord.utils.get(guild.text_channels, name=name)
                if target:
                    break
        target = target or active_text_channel
        if target is None:
            return
        await target.send(content="📓 馬文今日漫畫", file=discord.File(out))
        logger.info(f"📓 [DiaryComic] 已貼漫畫 {out}（{used}）")
    except Exception as e:
        logger.warning(f"⚠️ [DiaryComic] 出漫畫失敗（已吞，不影響 loop）: {e}")

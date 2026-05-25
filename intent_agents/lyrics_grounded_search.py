"""Grounded lyrics-to-song identification — Gemini + Google Search tool.

取代 find_song_agent.py 既有 backend「LLM 從歌詞片段瞎猜」做法。原 backend 拿歌詞片段
直接問 LLM「這是哪首歌」，LLM 沒實際搜尋會自信幻覺出不存在的歌——尤其在 STT 把歌詞
聽糊掉時（e.g. 「升旗白馬過三觀」），LLM 仍胡謅「張學友 - 將軍令」。

本模組改走 Gemini 內建的 google_search tool（grounding），並要求：
  L1：response 開頭「無」→ 拒（Gemini 自承找不到）
  L2：grounding_metadata.grounding_chunks 必須非空 → 拒（Gemini 沒實際搜到網頁就在編）

兩條都通過才回「藝人 - 歌名」字串。Google 搜尋庫涵蓋 mojim/KKBOX/Genius/NetEase 全部
華語歌詞資料庫，coverage 比 LRC fetch 廣很多——這也是放棄 LRC 守門的理由。

INFO log 每個決策點，方便診斷上線狀況。
"""
from __future__ import annotations

import asyncio
import logging

import google.genai as genai

logger = logging.getLogger(__name__)


_PROMPT_TEMPLATE = (
    "歌詞片段來自語音辨識（STT），可能含同音字 / 近音字錯誤。\n"
    "例：「升旗白馬過三觀」實際應為「身騎白馬過三關」；「天清色」應為「天青色」。\n\n"
    "任務（依序執行）：\n"
    "1. 先用 Google 搜尋這個片段。\n"
    "2. 如果直接搜不到，把它視為 STT 錯誤，依拼音相近原則猜原句，再 Google 搜尋驗證。\n"
    "3. 必須在搜尋結果裡看到真實歌詞網站（mojim、KKBOX、Genius、NetEase、Spotify、"
    "AZLyrics 等）確實含有這段歌詞或修正後的版本，才能算找到。\n"
    "4. 不要從歌名 / vibe / 直覺猜——沒有實際 search hit 一律回「無」。\n\n"
    "輸出格式（嚴格遵守）：\n"
    "  找到 → 一行：藝人 - 歌名\n"
    "  找不到（含修正後仍找不到）→ 一行：無\n"
    "不要加引號、解釋、JSON、emoji、多行。\n\n"
    "歌詞片段（可能含 STT 錯誤）：{fragment}"
)


def _extract_chunks(response) -> list:
    """從 response 抽 grounding_chunks，任何結構不全的情況回 []。"""
    try:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return []
        gm = getattr(candidates[0], "grounding_metadata", None)
        if gm is None:
            return []
        chunks = getattr(gm, "grounding_chunks", None) or []
        return list(chunks)
    except Exception:
        return []


async def search_lyrics_grounded(
    google_client,
    fragment: str,
    *,
    model: str = "gemini-2.5-flash",
    timeout: float = 15.0,
) -> str | None:
    """從歌詞片段識別歌曲，回傳「藝人 - 歌名」或 None。

    google_client: genai.Client 實例（通常為 bot.router.google_client）。None → 直接回 None。
    fragment: 歌詞片段；空字串 / 純空白 → 直接回 None，不打 API。

    驗證：見 module docstring。任一驗證失敗都回 None。
    """
    if not fragment or not fragment.strip():
        return None
    if google_client is None:
        logger.info("[LyricsGrounded] google_client 是 None，跳過 grounded（fallback 路徑接手）")
        return None

    frag_clean = fragment.strip()
    prompt = _PROMPT_TEMPLATE.format(fragment=frag_clean)

    try:
        config = genai.types.GenerateContentConfig(
            tools=[genai.types.Tool(google_search=genai.types.GoogleSearch())],
            temperature=0.2,
        )
        response = await asyncio.wait_for(
            google_client.aio.models.generate_content(
                model=model,
                contents=prompt,
                config=config,
            ),
            timeout=timeout,
        )
    except Exception as e:
        logger.warning(f"[LyricsGrounded] API 例外 fragment={frag_clean!r}: {type(e).__name__}: {e}")
        return None

    text = (getattr(response, "text", None) or "").strip()
    if not text:
        logger.info(f"[LyricsGrounded] empty response fragment={frag_clean!r}")
        return None

    first_line = text.splitlines()[0].strip()
    if not first_line:
        logger.info(f"[LyricsGrounded] empty first line fragment={frag_clean!r}")
        return None

    # L1: Gemini 自承找不到
    if first_line.startswith("無"):
        logger.info(f"[LyricsGrounded] L1 拒（LLM 回無）fragment={frag_clean!r}")
        return None

    # L2: grounding_chunks 必須非空，否則 Gemini 沒實際搜到網頁
    chunks = _extract_chunks(response)
    if not chunks:
        logger.warning(
            f"[LyricsGrounded] L2 拒（grounding_chunks 空，疑似幻覺）"
            f" fragment={frag_clean!r} ident={first_line!r}"
        )
        return None

    # 記錄 chunk domain + title，方便診斷 Gemini 同音字修正後到底匹配了哪首歌
    sources = []
    for c in chunks[:5]:
        uri = getattr(c, "uri", "") or ""
        title = (getattr(c, "title", "") or "")[:60]
        try:
            host = uri.split("/")[2] if "//" in uri else uri[:40]
        except Exception:
            host = "?"
        sources.append(f"{host} ({title})")
    logger.info(
        f"[LyricsGrounded] ✓ fragment={frag_clean!r} → {first_line!r}"
        f" (chunks={len(chunks)}, src={sources})"
    )
    return first_line

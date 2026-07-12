"""DJ 播報用的乾淨歌名抽取（純函式）。

Why：YouTube 影片標題常又長又髒（【官方MV】…歌詞版 HD），DJ 直接念會超時、A→B 推薦
理由更被撐成雙倍。本 module 只給 **DJ 語音路徑** 用——歌詞查詢仍走
`MusicCog._parse_song_title_artist`（lrclib 需分開的 track/artist，catalog 的
"藝人 歌名" 合併格式不適合歌詞查詢）。

三層優先序：
  ① info['track']（yt-dlp / YouTube Music 結構化欄位，最乾淨）
  ② catalog videoId 精確查（records/music_catalog.json 的結構化 name，含藝人）
  ③ raw title regex 剝雜訊標記（括號 tag + 分隔/空白後的常見 cruft 關鍵字）
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

# 已知 cruft 關鍵字（Official MV / HD / 歌詞版 / feat 名單…）
_CRUFT_KW = (
    r"official|m/?v|hd|hq|4k|8k|lyrics?|audio|music|video|live|remix|full(?:\s*version)?|"
    r"完整版|高清\S*|官方\S*|歌詞\S*|字幕\S*|純享\S*|現場\S*|無損|演唱會|"
    r"cover|feat\.?.*|ft\.?.*"
)
_CRUFT_IN = re.compile(_CRUFT_KW, re.I)

# 括號整段（【】[]()（）「」）——內容含 cruft → 整段丟；否則只脫殼保留內容
# （中文 YouTube 常把「歌名」放 【】 裡，不能無腦剝掉）
_BRACKET_ANY = re.compile(r"[【\[（(「]([^】\]）)」]*)[】\]）)」]")

# 尾端 cruft：分隔符或空白 + 已知關鍵字 → 到結尾。反覆套用剝多個尾巴。
_TAIL = re.compile(rf"(?i)[\s|｜/／\-–—•·]+(?:{_CRUFT_KW})\s*$")


def clean_title_regex(raw: str) -> str:
    """剝掉 raw 標題的雜訊括號與尾端 cruft；清成空則回原字串（fail-safe）。

    ⚠️ 含 cruft 的括號才整段丟；其餘括號只脫殼保留內容（歌名常在 【】 裡）。
    """
    if not raw:
        return raw

    def _debracket(m):
        content = m.group(1)
        return " " if _CRUFT_IN.search(content) else f" {content} "

    s = _BRACKET_ANY.sub(_debracket, raw)
    prev = None
    while prev != s:  # 反覆剝尾（" | 歌詞版 | HD" 這種多段）
        prev = s
        s = _TAIL.sub("", s).strip()
    s = re.sub(r"\s+", " ", s).strip(" -|｜/／–—•·")
    # fail-safe：清成空或只剩零碎標點（無字母/數字/中日韓字）→ 回原字串，別回垃圾
    return s if re.search(r"[0-9A-Za-z぀-ヿ一-鿿]", s) else raw.strip()


@lru_cache(maxsize=1)
def _default_catalog_index(path: str = "records/music_catalog.json") -> dict:
    """videoId → 結構化 name（"藝人 歌名"）。載入失敗回空 dict（graceful）。"""
    try:
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
        return {r["videoId"]: r["name"] for r in rows if r.get("videoId") and r.get("name")}
    except Exception:
        return {}


def dj_display_name(
    info: dict,
    *,
    extract_vid: Callable[[str], Optional[str]],
    catalog_index: Optional[dict] = None,
) -> tuple[str, str]:
    """回 (title, artist) 給 DJ 播報。catalog 命中時 artist 回 ""（name 已含藝人，
    交由 no-artist 台詞模板處理）。catalog_index=None → 用預設檔案索引。"""
    track = (info.get("track") or "").strip()
    if track:
        return track, (info.get("artist") or "").strip()

    idx = _default_catalog_index() if catalog_index is None else catalog_index
    vid = extract_vid(info.get("webpage_url") or info.get("url") or info.get("id") or "")
    if vid and vid in idx:
        return idx[vid].strip(), ""

    raw = info.get("title", "") or ""
    cleaned = clean_title_regex(raw)
    if " - " in cleaned:  # 保留 "Artist - Title" 拆分（剝雜訊後才拆）
        artist, title = cleaned.split(" - ", 1)
        return title.strip(), artist.strip()
    return cleaned, (info.get("artist") or info.get("uploader") or "").strip()

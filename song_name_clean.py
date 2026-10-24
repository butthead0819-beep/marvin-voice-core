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
_BRACKET_PAIRS = (("【", "】"), ("[", "]"), ("（", "）"), ("(", ")"), ("「", "」"))
_BRACKET_ANY = re.compile(
    "|".join(rf"{re.escape(o)}([^{re.escape(c)}]*){re.escape(c)}" for o, c in _BRACKET_PAIRS)
)
_BRACKET_STRIP_CHARS = "".join(o + c for o, c in _BRACKET_PAIRS) + " |｜/／\\-–—•·.．"


def _is_pure_cruft(content: str) -> bool:
    """整段括號內容是否「整體」就是雜訊標記（如 Official MV/HD），而非「含有」雜訊子字串。
    先挖掉已知 cruft 關鍵字與括號/分隔符號，若殘留無字母數字/中日韓字 → 判定整段皆雜訊。
    """
    stripped = _CRUFT_IN.sub("", content)
    stripped = re.sub(rf"[{re.escape(_BRACKET_STRIP_CHARS)}]+", "", stripped)
    return not re.search(r"[0-9A-Za-z぀-ヿ一-鿿]", stripped)

# 尾端 cruft：分隔符或空白 + 已知關鍵字 → 到結尾。反覆套用剝多個尾巴。
_TAIL = re.compile(rf"(?i)[\s|｜/／\-–—•·]+(?:{_CRUFT_KW})\s*$")


def clean_title_regex(raw: str) -> str:
    """剝掉 raw 標題的雜訊括號與尾端 cruft；清成空則回原字串（fail-safe）。

    ⚠️ 含 cruft 的括號才整段丟；其餘括號只脫殼保留內容（歌名常在 【】 裡）。
    """
    if not raw:
        return raw

    def _debracket(m):
        content = next(g for g in m.groups() if g is not None)
        return " " if _is_pure_cruft(content) else f" {content} "

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


_GENERIC_TOPIC_UPLOADERS = {"release"}


def _artist_from_uploader(uploader: str) -> str:
    """YouTube 自動頻道「XX - Topic」→ XX；XX 不是歌手（Release）→ ""。"""
    name = (uploader or "").strip()
    if name.endswith(" - Topic"):
        name = name[: -len(" - Topic")].strip()
        if name.lower() in _GENERIC_TOPIC_UPLOADERS:
            return ""
    return name


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
    return cleaned, _artist_from_uploader(info.get("artist") or info.get("uploader") or "")


# TTS 念歌名的最後一道閘（2026-10-24）：清洗後仍像 YouTube 標題就不念。
_UNSPEAKABLE_TITLE = re.compile(
    r"[『』【】《》「」\"|｜#@♪♫]|official|music\s*video|lyrics?|動態歌詞|歌詞版|歌词版|高音質|高音质|"
    r"無損|无损|完整版|官方|\bMV\b|\bHD\b|cover\s+by|pinyin|vietsub|原唱|\bOST\b|"
    r"片頭曲|片尾曲|插曲|主題曲|主题曲|純享|纯享|現場版|现场版|\btopic\b",
    re.I,
)
_UNSPEAKABLE_ARTIST = re.compile(
    r"topic|頻道|频道|官方|channel|視頻|视频|小站|資訊站|资讯站|lyric|music|learning|studio|records|"
    r"唱片|娛樂|娱乐|音樂|音乐|\bTV\b|official|[|｜#@]|^release$",
    re.I,
)
_EMOJI = re.compile(r"[\U0001F000-\U0001FAFF☀-➿]")
# 尾端括號是劇名/主題曲/feat 註記 → 先剝掉再判（「一念(電視劇《逐玉》插曲)」→「一念」）
_TAIL_NOTE = re.compile(
    r"\s*[(（][^()（）]*(?:《|\"|主題曲|主题曲|插曲|片頭曲|片尾曲|慟情曲|OST|feat\.?|ft\.)[^()（）]*[)）]\s*$", re.I)
_CJK = re.compile(r"[぀-ヿ㐀-鿿豈-﫿가-힯]")
SPOKEN_TITLE_MAX_CJK = 16   # 中日韓字超過這個數＝標題塞了歌詞
SPOKEN_TITLE_MAX = 40
SPOKEN_ARTIST_MAX = 20


def speakable_title(title) -> str | None:
    t = _TAIL_NOTE.sub("", (title or "").strip()).strip()
    if (not t or len(t) > SPOKEN_TITLE_MAX or len(_CJK.findall(t)) > SPOKEN_TITLE_MAX_CJK
            or _UNSPEAKABLE_TITLE.search(t) or _EMOJI.search(t)):
        return None
    return t


def speakable_artist(artist) -> str:
    a = (artist or "").strip()
    if not a or len(a) > SPOKEN_ARTIST_MAX or _UNSPEAKABLE_ARTIST.search(a) or _EMOJI.search(a):
        return ""
    return a


def spoken_name(title, artist) -> tuple[str | None, str]:
    """(歌名, 歌手) → 念得出口的版本；歌名念不出口回 (None, "")，呼叫端就不提歌名。"""
    t = speakable_title(title)
    if t is None:
        return None, ""
    return t, speakable_artist(artist)


def spoken_song_name(info: dict, *, extract_vid, catalog_index=None) -> tuple[str | None, str]:
    """info → 念得出口的 (歌名, 歌手)：_canon 正規化優先，否則 dj_display_name，再過閘。
    與 MusicCog._dj_clean_name 同一優先序（給拿不到 MusicCog 的 voice_controller 用）。"""
    canon = info.get("_canon") or {}
    if canon.get("title") and canon.get("artist"):
        return spoken_name(canon["title"], canon["artist"])
    return spoken_name(*dj_display_name(info, extract_vid=extract_vid, catalog_index=catalog_index))

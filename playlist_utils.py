"""🎵 playlist_utils — 個人歌單解析與匯出格式化工具模組。

提供歌單格式化（TXT, JSON, CSV）、檔案與字串解析、以及 YouTube 播放清單 fast-flat 快速提取。
"""
from __future__ import annotations

import csv
import io
import json
import re
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_YT_PLAYLIST_RE = re.compile(
    r"(?:https?://)?(?:www\.|music\.)?youtube\.com/(?:playlist\?list=|watch\?.*[&?]list=)([A-Za-z0-9_-]+)"
)
_URL_IN_PAREN_RE = re.compile(r"\((https?://[^\s)]+)\)")
_URL_GENERIC_RE = re.compile(r"https?://[^\s)]+")


def is_youtube_playlist_url(url: str) -> bool:
    """判斷字串是否包含 YouTube 播放清單 (playlist) 連結。"""
    if not url or not isinstance(url, str):
        return False
    return bool(_YT_PLAYLIST_RE.search(url))


def format_playlist_text(songs: list[dict]) -> str:
    """將歌曲清單格式化為易讀的純文字清單。"""
    lines = []
    for idx, s in enumerate(songs, 1):
        title = s.get("title") or "未知歌曲"
        uploader = s.get("uploader")
        url = s.get("webpage_url") or s.get("url") or ""
        
        artist_part = f" - {uploader}" if uploader and uploader != "Unknown" else ""
        url_part = f" ({url})" if url else ""
        lines.append(f"{idx}. {title}{artist_part}{url_part}")
    return "\n".join(lines)


def format_playlist_json(songs: list[dict]) -> str:
    """將歌曲清單格式化為結構化 JSON 字串。"""
    return json.dumps(songs, ensure_ascii=False, indent=2)


def format_playlist_csv(songs: list[dict]) -> str:
    """將歌曲清單格式化為 CSV 字串。"""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["title", "uploader", "url", "user_plays", "total_plays", "liked"])
    for s in songs:
        writer.writerow([
            s.get("title", ""),
            s.get("uploader", ""),
            s.get("webpage_url") or s.get("url", ""),
            s.get("user_plays", 1),
            s.get("total_plays", 1),
            "true" if s.get("liked") else "false",
        ])
    return output.getvalue()


def format_playlist_export(songs: list[dict], fmt: str = "txt", username: str = "User") -> tuple[str, bytes, str]:
    """產出指定格式的歌單匯出資料。

    回傳: (文字摘要, 檔案 bytes, 副檔名)
    """
    fmt = (fmt or "txt").lower().strip()
    if fmt == "json":
        content = format_playlist_json(songs)
        ext = "json"
    elif fmt == "csv":
        content = format_playlist_csv(songs)
        ext = "csv"
    else:
        content = format_playlist_text(songs)
        ext = "txt"

    summary = f"📋 **【{username} 的個人歌單】**（共 {len(songs)} 首，格式：{ext.upper()}）"
    return summary, content.encode("utf-8"), ext


def parse_playlist_content(content: str | bytes, file_ext_or_type: str = "txt") -> list[dict]:
    """解析檔案內容或文字字串為標準歌曲字典清單。"""
    if isinstance(content, bytes):
        text = content.decode("utf-8", errors="replace")
    else:
        text = str(content)

    file_ext_or_type = (file_ext_or_type or "txt").lower().strip()

    if file_ext_or_type == "json":
        try:
            data = json.loads(text)
            if isinstance(data, list):
                out = []
                for item in data:
                    if isinstance(item, dict):
                        out.append({
                            "title": item.get("title", ""),
                            "uploader": item.get("uploader") or item.get("channel", ""),
                            "webpage_url": item.get("webpage_url") or item.get("url", ""),
                            "url": item.get("url") or item.get("webpage_url", ""),
                            "liked": bool(item.get("liked", False)),
                        })
                    elif isinstance(item, str) and item.strip():
                        out.append({"title": item.strip(), "webpage_url": item.strip() if item.startswith("http") else ""})
                return [s for s in out if s["title"] or s["webpage_url"]]
        except Exception as e:
            logger.warning(f"JSON 歌單解析失敗: {e}")

    if file_ext_or_type == "csv":
        try:
            reader = csv.reader(io.StringIO(text))
            header = next(reader, None)
            out = []
            if header:
                h_lower = [h.strip().lower() for h in header]
                title_idx = h_lower.index("title") if "title" in h_lower else 0
                uploader_idx = h_lower.index("uploader") if "uploader" in h_lower else (1 if len(h_lower) > 1 else -1)
                url_idx = h_lower.index("url") if "url" in h_lower else (2 if len(h_lower) > 2 else -1)

                for row in reader:
                    if not row:
                        continue
                    t = row[title_idx].strip() if 0 <= title_idx < len(row) else ""
                    u = row[uploader_idx].strip() if 0 <= uploader_idx < len(row) else ""
                    url_val = row[url_idx].strip() if 0 <= url_idx < len(row) else ""
                    if t or url_val:
                        out.append({
                            "title": t,
                            "uploader": u,
                            "webpage_url": url_val,
                            "url": url_val,
                        })
            return out
        except Exception as e:
            logger.warning(f"CSV 歌單解析失敗: {e}")

    # 預設為 TXT / 多行文字解析
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        # 移除行首編號（例如 "1. ", "01. ", "[1] "）
        cleaned = re.sub(r"^(?:\[?\d+[\].)\-]\s*)+", "", line).strip()
        if not cleaned:
            continue

        # 尋找括號中的 URL 或行內的 URL
        url_match = _URL_IN_PAREN_RE.search(cleaned) or _URL_GENERIC_RE.search(cleaned)
        url_val = url_match.group(1) if url_match and url_match.re == _URL_IN_PAREN_RE else (url_match.group(0) if url_match else "")
        
        # 取得剩餘文字作為歌名與藝人
        text_without_url = _URL_IN_PAREN_RE.sub("", cleaned)
        text_without_url = _URL_GENERIC_RE.sub("", text_without_url).strip(" -()[]")

        uploader = ""
        title = text_without_url
        if " - " in text_without_url:
            parts = text_without_url.split(" - ", 1)
            uploader, title = parts[0].strip(), parts[1].strip()

        if not title and url_val:
            title = url_val

        if title or url_val:
            out.append({
                "title": title,
                "uploader": uploader,
                "webpage_url": url_val,
                "url": url_val,
            })
    return out


async def extract_youtube_playlist_flat(playlist_url: str) -> list[dict]:
    """快速扁平提取 YouTube 播放清單內所有歌曲（不觸發完整下載/串流解析）。"""
    import yt_dlp

    ydl_opts = {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
    }

    def _extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(playlist_url, download=False)

    try:
        info = await asyncio.to_thread(_extract)
    except Exception as e:
        logger.error(f"❌ [PlaylistUtils] yt-dlp playlist 擷取失敗: {e}")
        return []

    if not info:
        return []

    entries = info.get("entries") or []
    songs = []
    for e in entries:
        if not e:
            continue
        vid = e.get("id")
        title = e.get("title") or "Unknown"
        uploader = e.get("uploader") or e.get("channel") or ""
        wp = e.get("url") or e.get("webpage_url")
        if not wp and vid:
            wp = f"https://www.youtube.com/watch?v={vid}"

        songs.append({
            "title": title,
            "uploader": uploader,
            "webpage_url": wp or "",
            "url": wp or "",
        })
    return songs

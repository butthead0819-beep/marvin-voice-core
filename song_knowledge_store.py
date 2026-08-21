"""本地歌曲深度知識庫快取與整合模組。

純本地 JSON 快取（records/song_knowledge.json）：
1. 查過一次永久快取，秒讀零開銷
2. 整合 YouTube 簡介抽取器與 Wikipedia 補全
3. 提供乾淨的音樂賞析（製作幕後、創作故事）格式化文字
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from song_metadata_extractor import extract_song_metadata_from_description
from wikipedia_music_fetcher import fetch_wikipedia_music_summary

logger = logging.getLogger(__name__)

DEFAULT_PATH = "records/song_knowledge.json"


def format_music_insight(data: dict[str, Any] | None) -> str | None:
    """將歌曲知識結構格式化為注入 LLM Context 的賞析字串。"""
    if not data:
        return None

    parts = []
    
    # 1. 製作幕後名單
    lyricist = data.get("lyricist", "").strip()
    composer = data.get("composer", "").strip()
    album = data.get("album", "").strip()
    
    credits_parts = []
    if lyricist and composer and lyricist == composer:
        credits_parts.append(f"{lyricist}詞曲創作")
    else:
        if lyricist:
            credits_parts.append(f"{lyricist}作詞")
        if composer:
            credits_parts.append(f"{composer}作曲")
    
    if credits_parts:
        credit_str = "、".join(credits_parts)
        if album:
            credit_str += f"（收錄於《{album}》）"
        parts.append(f"製作幕後：{credit_str}")
    elif album:
        parts.append(f"收錄專輯：《{album}》")

    # 2. 創作背景故事
    story = data.get("story_snippet", "").strip()
    if story:
        parts.append(f"歌曲背景：{story}")

    if not parts:
        return None

    return " · ".join(parts)


class SongKnowledgeStore:
    def __init__(self, path: str = DEFAULT_PATH):
        self._path = path
        self._data = self._load()

    def _load(self) -> dict[str, dict]:
        try:
            if os.path.exists(self._path):
                with open(self._path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.debug(f"[SongKnowledgeStore] 讀取 {self._path} 失敗: {e}")
        return {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = f"{self._path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception as e:
            logger.debug(f"[SongKnowledgeStore] 寫入 {self._path} 失敗: {e}")

    def get(self, key: str) -> dict | None:
        return self._data.get(key)

    def set(self, key: str, data: dict) -> None:
        if not key or not data:
            return
        self._data[key] = data
        self._save()

    async def get_or_extract_insight(
        self,
        info: dict,
        clean_title: str = "",
        clean_artist: str = "",
    ) -> str | None:
        """取得或非同步提取該歌曲的音樂深度知識，回傳格式化字串。"""
        title = clean_title or info.get("title", "")
        artist = clean_artist or ""
        key = f"{artist} - {title}" if artist else title
        if not key:
            return None

        # 1. 檢查本地快取
        cached = self.get(key)
        if cached:
            return format_music_insight(cached)

        # 2. 從 YouTube 簡介抽取
        desc = info.get("description", "")
        extracted = extract_song_metadata_from_description(desc)

        # 3. 若無故事，嘗試從維基百科補全
        if not extracted.get("story_snippet") and title:
            try:
                wiki_summary = await fetch_wikipedia_music_summary(title, artist)
                if wiki_summary:
                    extracted["story_snippet"] = wiki_summary
            except Exception:
                pass

        if extracted:
            self.set(key, extracted)
            return format_music_insight(extracted)

        return None

"""DJ 串場話題選擇器：把「近期生活」「在場興趣」拆成獨立話題來源，每則播報只挑一個，
挑中的具體話題冷卻 8 小時內不重複——治「近期生活每 5 分鐘就提一次」的重複感。

純函式 + disk JSON（撓過重啟），fail-open：壞檔/IO 失敗當空冷卻表，不擋 DJ 生成。

meme_id 語義冷卻：同一事件換個說法也算冷卻中（不能用文字 SHA1 繞過）。
  is_cool(text, meme_id=X) / mark_used(text, meme_id=X)
  meme_id 用 "meme:{meme_id}" 作 key，與純文字 SHA1 key 是分離的 namespace。
"""
from __future__ import annotations

import hashlib
import json
import os
import time

DEFAULT_PATH = "records/dj_topic_cooldown.json"
COOLDOWN_S = 8 * 3600  # 同一具體話題用過 8 小時內不重複


def _topic_key(text: str) -> str:
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:16]


class TopicCooldownStore:
    def __init__(self, path: str = DEFAULT_PATH, *, now=time.time):
        self._path = path
        self._now = now
        self._data = self._load()

    def _load(self) -> dict:
        try:
            return json.load(open(self._path, encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = f"{self._path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False)
            os.replace(tmp, self._path)
        except OSError:
            pass  # fail-open：寫不進去不影響功能（下次再判斷）

    def is_cool(self, text: str, *, meme_id: str | None = None) -> bool:
        """話題是否可用（沒用過，或用過但已超過冷卻時間）。

        meme_id: 語義 tag。傳入時用 "meme:{meme_id}" 作 key，
                 與純 text hash 是獨立 namespace，互不影響。
        """
        key = f"meme:{meme_id}" if meme_id else _topic_key(text)
        ts = self._data.get(key)
        if ts is None:
            return True
        return self._now() - ts >= COOLDOWN_S

    def mark_used(self, text: str, *, meme_id: str | None = None) -> None:
        key = f"meme:{meme_id}" if meme_id else _topic_key(text)
        self._data[key] = self._now()
        self._save()


def select_topic(
    life_cores: list[str | tuple[str, str]],
    interests: list[str],
    store: TopicCooldownStore,
) -> tuple[str | None, str]:
    """依序挑：近期生活 → 在場興趣 → 無話題（純過場，caller 該退回歌曲間銜接詞）。

    回傳 (topic_text, topic_type)，topic_type in {'life', 'interest', 'none'}。
    挑中的話題視為即將被用掉，立刻標記冷卻。

    life_cores 每項可以是：
      - str                → 純文字，用 SHA1 hash 冷卻（舊介面，向後相容）
      - (text, meme_id)    → 帶語義 tag，用 meme_id 冷卻（同 meme 換說法也算冷卻）
    """
    for item in life_cores or []:
        if isinstance(item, tuple):
            c, meme_id = item[0], item[1]
        else:
            c, meme_id = item, None
        c = (c or "").strip()
        if c and store.is_cool(c, meme_id=meme_id):
            store.mark_used(c, meme_id=meme_id)
            return c, "life"
    for i in interests or []:
        i = (i or "").strip()
        if i and store.is_cool(i):
            store.mark_used(i)
            return i, "interest"
    return None, "none"

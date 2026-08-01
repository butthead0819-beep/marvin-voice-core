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

from dj_life_context import LifeCore

DEFAULT_PATH = "records/dj_topic_cooldown.json"
COOLDOWN_S = 8 * 3600  # 同一具體話題用過 8 小時內不重複

# 話題（life/interest）都沒有時，本地在這四種 fallback 之間輪替，別每次都落在
# 同一種（尤其是「環境/天氣」那種永遠在場的素材，之前是靠 LLM「自由發揮」硬凹，
# 結果每次都用它開場——現在改把它變成 atmosphere，跟其他 fallback 平等輪替，
# 不再是預設值）。atmosphere（時間/地點氛圍）永遠有素材可用，跟 quick 一樣不需要
# has_* 旗標。quick 沒有任何素材，caller 該走本地模板、跳過 LLM，排最後當墊底選項。
FALLBACK_ORDER = ("conversation", "prev_song", "atmosphere", "quick")
_FALLBACK_KEY = "_last_fallback_mode"


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

    def get_last_fallback(self) -> str | None:
        """上次選到的 fallback mode（conversation/prev_song/quick），跨重啟保存。"""
        return self._data.get(_FALLBACK_KEY)

    def set_last_fallback(self, mode: str) -> None:
        self._data[_FALLBACK_KEY] = mode
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


def _decompose_life_item(item) -> tuple[str, str, tuple[str, ...]]:
    """相容 LifeCore / (text, meme_id) / 純 str 三種形狀，統一拆成 (text, meme_id, speakers)。"""
    if isinstance(item, LifeCore):
        return item.text, item.meme_id, item.speakers
    if isinstance(item, tuple):
        return item[0], item[1], ()
    return item, "", ()


def _filter_present_actors(
    life_cores,
    present_members: set[str] | None,
) -> list[str | tuple[str, str]]:
    """事件主角現在不在場的生活素材直接濾掉（換下一個候選），別留給 LLM 自己猜代名詞。

    speakers 是空的（一般公開話題、沒有特定主角）一律放行——沒有主角就沒有代名詞
    掛錯的風險。present_members=None（vc 不可用）時不過濾，fail-open。
    """
    out: list[str | tuple[str, str]] = []
    for item in life_cores or []:
        text, meme_id, speakers = _decompose_life_item(item)
        if present_members is not None and speakers and not set(speakers).issubset(present_members):
            continue
        out.append((text, meme_id) if meme_id else text)
    return out


def _pick_fallback_mode(
    store: TopicCooldownStore,
    *,
    has_conversation: bool,
    has_prev_song: bool,
) -> str:
    candidates = [
        m for m in FALLBACK_ORDER
        if (m != "conversation" or has_conversation)
        and (m != "prev_song" or has_prev_song)
    ]
    if not candidates:
        candidates = ["quick"]
    last = store.get_last_fallback()
    mode = next((m for m in candidates if m != last), candidates[0])
    store.set_last_fallback(mode)
    return mode


def select_mode(
    life_cores: list,
    interests: list[str],
    store: TopicCooldownStore,
    *,
    present_members: set[str] | None = None,
    has_conversation: bool = False,
    has_prev_song: bool = False,
) -> tuple[str | None, str]:
    """本地決定這次串場要走哪個 mode，LLM 不必自己判斷「有沒有話題、要不要硬掰」。

    順序：近期生活（主角要在場）→ 在場興趣 → 都沒有時，在 conversation/prev_song/quick
    間輪替（避免每次都落在同一種 fallback，尤其是最容易變成「每次都環境/天氣」的那個）。

    回傳 (topic_text, mode)，mode 比 select_topic 多了 'conversation'/'prev_song'/'quick'。
    topic_text 只有 mode in {'life', 'interest'} 才非 None，其餘三種 fallback 沒有具體
    文字素材——caller 自己依 mode 決定串場方向（quick 甚至該跳過 LLM，直接走本地模板）。
    """
    filtered_life = _filter_present_actors(life_cores, present_members)
    topic, kind = select_topic(filtered_life, interests, store)
    if kind != "none":
        return topic, kind
    return None, _pick_fallback_mode(
        store, has_conversation=has_conversation, has_prev_song=has_prev_song,
    )

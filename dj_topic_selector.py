"""DJ 串場話題選擇器：把「近期生活」「在場興趣」拆成獨立話題來源，每則播報只挑一個，
挑中的具體話題冷卻 8 小時內不重複——治「近期生活每 5 分鐘就提一次」的重複感。

純函式 + disk JSON（撓過重啟），fail-open：壞檔/IO 失敗當空冷卻表，不擋 DJ 生成。

meme_id 語義冷卻：同一事件換個說法也算冷卻中（不能用文字 SHA1 繞過）。
  is_cool(text, meme_id=X) / mark_used(text, meme_id=X)
  meme_id 用 "meme:{meme_id}" 作 key，與純文字 SHA1 key 是分離的 namespace。
"""
from __future__ import annotations

import hashlib
import time

from dj_life_context import LifeCore
from state_store import StateStore

DEFAULT_PATH = "records/dj_topic_cooldown.json"
COOLDOWN_S = 8 * 3600       # 同一具體生活/興趣話題用過 8 小時內不重複
NEWS_COOLDOWN_S = 2 * 3600  # 新聞頻率可較高，2 小時內不重複

# 話題（life/interest/news）都沒有時，本地在這四種 fallback 之間輪替，別每次都落在
# 同一種（尤其是「環境/天氣」那種永遠在場的素材，之前是靠 LLM「自由發揮」硬凹，
# 結果每次都用它開場——現在改把它變成 atmosphere，跟其他 fallback 平等輪替，
# 不再是預設值）。atmosphere（時間/地點氛圍）永遠有素材可用，跟 quick 一樣不需要
# has_* 旗標。quick 沒有任何素材，caller 該走本地模板、跳過 LLM，排最後當墊底選項。
FALLBACK_ORDER = ("conversation", "prev_song", "atmosphere", "quick")
_FALLBACK_KEY = "_last_fallback_mode"


def _topic_key(text: str) -> str:
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:16]


class TopicCooldownStore:
    """話題冷卻表，底層改用共用的 `state_store.StateStore`（Phase B 遷移）。

    讀取策略：`self._data` 是 in-memory cache，只在 `__init__`（構造當下 load
    一次）跟「這個 instance 自己做過 mutation 之後」（`mark_used` /
    `set_last_fallback`，見下方）更新——`is_cool()` / `get_last_fallback()`
    這類純讀取一律吃 cache，不每次重新 open 檔案。理由：
      1. 跟遷移前的行為一致（原本就是建構時 load 一次進 `self._data`），
         呼叫端（`select_topic`/`select_mode`）的既有測試（例如
         `test_cooldown_persists_across_store_instances`：重啟＝重新
         construct 一個新 instance 才會看到別人的更新）已經預設這個語意，
         沒有理由這次遷移順便改變它。
      2. 實務上呼叫端幾乎都是「一次串場決策內」建構/複用同一個 instance，
         連續呼叫多次 `is_cool()`，這段期間頻繁 open+flock+json.load 只有
         成本沒有效益。

    但寫入路徑（`mark_used()`）不能只改自己這份 cache 再整份 dump 回去
    ——那正是舊實作的 race 來源（見 module docstring 引用的
    feedback_music_memory_concurrent_write_race）：兩個 process/instance
    各自 load 一次、各自 mutate、後寫的會蓋掉先寫的。改用
    `StateStore.update(fn)` 之後，每次 `mark_used()` 都在拿到 flock 之後
    重新從 disk load「當下最新」的完整資料、套用這次的 mutation、再存回
    去——不管呼叫方自己那份 `self._data` cache 多舊，寫回去的一定是「最新
    資料 + 這次的變更」，不會丟失其他 instance/process 同時寫入的內容。
    寫完之後才用 `update()` 的回傳值刷新 `self._data`，讓這個 instance
    後續的 `is_cool()` 讀到自己剛寫的東西。
    """

    def __init__(self, path: str = DEFAULT_PATH, *, now=time.time):
        self._path = path
        self._now = now
        self._store = StateStore(path, default={})
        self._data = self._store.load()

    def _mutate(self, fn) -> None:
        """鎖保護的 read-modify-write：fn 吃「當下最新的完整資料」、回傳新資料。"""
        self._data = self._store.update(lambda current: fn(dict(current) if isinstance(current, dict) else {}))

    def is_cool(
        self,
        text: str,
        *,
        meme_id: str | None = None,
        cooldown_s: float | None = None,
    ) -> bool:
        """話題是否可用（沒用過，或用過但已超過冷卻時間）。

        meme_id: 語義 tag。傳入時用 "meme:{meme_id}" 作 key，
                 與純 text hash 是獨立 namespace，互不影響。
        cooldown_s: 自訂冷卻秒數（預設使用 COOLDOWN_S 8小時，新聞為 2 小時）。
        """
        cd = cooldown_s if cooldown_s is not None else COOLDOWN_S
        key = f"meme:{meme_id}" if meme_id else _topic_key(text)
        ts = self._data.get(key)
        if ts is None:
            return True
        return self._now() - ts >= cd

    def mark_used(
        self,
        text: str,
        *,
        meme_id: str | None = None,
        cooldown_s: float | None = None,
    ) -> None:
        key = f"meme:{meme_id}" if meme_id else _topic_key(text)
        ts = self._now()

        def _apply(data: dict) -> dict:
            data[key] = ts
            return data

        self._mutate(_apply)

    def get_last_fallback(self) -> str | None:
        """上次選到的 fallback mode（conversation/prev_song/quick），跨重啟保存。"""
        return self._data.get(_FALLBACK_KEY)

    def set_last_fallback(self, mode: str) -> None:
        def _apply(data: dict) -> dict:
            data[_FALLBACK_KEY] = mode
            return data

        self._mutate(_apply)


def select_topic(
    life_cores: list[str | tuple[str, str]],
    interests: list[str],
    store: TopicCooldownStore,
    emotional_highlights: list[str] | None = None,
    news_items: list[str] | None = None,
) -> tuple[str | None, str]:
    """依序挑：近期生活 → 在場興趣 → 情緒高光 → 新聞快訊 → 無話題（純過場，caller 該退回歌曲間銜接詞）。

    回傳 (topic_text, topic_type)，topic_type in {'life', 'interest',
    'emotional_highlight', 'news', 'none'}。挑中的話題視為即將被用掉，立刻標記冷卻。
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
    for h in emotional_highlights or []:
        h = (h or "").strip()
        if h and store.is_cool(h):
            store.mark_used(h)
            return h, "emotional_highlight"
    for n in news_items or []:
        n = (n or "").strip()
        if n and store.is_cool(n, cooldown_s=NEWS_COOLDOWN_S):
            store.mark_used(n, cooldown_s=NEWS_COOLDOWN_S)
            return n, "news"
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
    emotional_highlights: list[str] | None = None,
    news_items: list[str] | None = None,
) -> tuple[str | None, str]:
    """本地決定這次串場要走哪個 mode，LLM 不必自己判斷「有沒有話題、要不要硬掰」。

    順序：近期生活（主角要在場）→ 在場興趣 → 情緒高光 → 新聞快訊 → 都沒有時，在
    conversation/prev_song/quick 間輪替（避免每次都落在同一種 fallback，尤其是最
    容易變成「每次都環境/天氣」的那個）。

    回傳 (topic_text, mode)，mode 比 select_topic 多了 'conversation'/'prev_song'/'quick'。
    topic_text 只有 mode in {'life', 'interest', 'emotional_highlight', 'news'} 才非 None，
    其餘三種 fallback 沒有具體文字素材——caller 自己依 mode 決定串場方向（quick
    甚至該跳過 LLM，直接走本地模板）。
    """
    filtered_life = _filter_present_actors(life_cores, present_members)
    topic, kind = select_topic(
        filtered_life,
        interests,
        store,
        emotional_highlights=emotional_highlights,
        news_items=news_items,
    )
    if kind != "none":
        return topic, kind
    return None, _pick_fallback_mode(
        store, has_conversation=has_conversation, has_prev_song=has_prev_song,
    )

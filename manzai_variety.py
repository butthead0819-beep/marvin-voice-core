"""漫才變化性：Marvin 的 ボケ 手法輪替 + 最近用過句子的 no-repeat ring buffer。

Why：prompt 只寫死一種手法（存在主義虛無）+ 每次呼叫互相獨立，輸出就退化成
單一模板——2026-09-22 實測五次不同話題，Marvin 五次都是「我只是一堆代碼 + X 也是
虛無」。這裡把手法拆成五種本地輪替（零 LLM 成本，仿 `dj_topic_selector.select_mode`），
再把最近用過的句子餵回 prompt 當禁用清單。

純函式 + disk JSON（撐過重啟），fail-open：壞檔/IO 失敗當空資料，絕不擋住漫才生成。
"""
from __future__ import annotations

import time

from state_store import StateStore

DEFAULT_PATH = "records/manzai_variety.json"
RING_SIZE = 6  # 最近幾句進 prompt 的禁用清單

_LAST_MODE_KEY = "_last_boke_mode"
_RECENT_KEY = "_recent_lines"  # 新→舊

BOKE_MODES = ("existential", "literal", "hyper_precise", "epic", "self_deprecate")

# 每段都是塞進 system prompt 的手法指示。**五段必須互斥**：一個 mode 的特徵詞不可
# 出現在別的 mode，否則 prompt 又把手法糊成一團，等於沒輪替
# （tests/test_manzai_variety.py::test_boke_blocks_are_mutually_exclusive 守這條）。
_BOKE_BLOCKS: dict[str, str] = {
    "existential": (
        "Marvin（馬文＝ボケ）：第一人稱接話，把這件事抽離成虛無——"
        "冷冷地指出它終將消散、沒有人會記得。越沉重越好（製造「緊張」讓 Marmo 來釋放）。"
    ),
    "literal": (
        "Marvin（馬文＝ボケ）：第一人稱接話，一本正經地照「字面意思」誤解對方講的某個詞，"
        "然後認真回答那個錯掉的意思。全程不知道自己會錯意，語氣越正經越好。"
    ),
    "hyper_precise": (
        "Marvin（馬文＝ボケ）：第一人稱接話，報出沒人問的、精確到小數點的數字或統計，"
        "像在念規格表。數字要跟話題沾得上邊但完全不是對方想知道的。"
    ),
    "epic": (
        "Marvin（馬文＝ボケ）：第一人稱接話，把這件日常小事講成史詩級的重大事件——"
        "用誇張的壯闊口吻描述它，彷彿攸關存亡。"
    ),
    "self_deprecate": (
        "Marvin（馬文＝ボケ）：第一人稱接話，拿自己行星級的大腦跟眼前這件瑣事做荒謬對比，"
        "自貶被指派來處理這種事。重點在落差，不在抱怨。"
    ),
}


class ManzaiVarietyStore:
    """輪替狀態 + 最近句子的持久化。

    讀走建構時 load 的 in-memory cache、寫一律走 `StateStore.update(fn)` 的
    read-modify-write（理由同 `dj_topic_selector.TopicCooldownStore`：整份 dump
    回去會被同時寫入的別的 process 互蓋）。
    """

    def __init__(self, path: str = DEFAULT_PATH, *, now=time.time):
        self._path = path
        self._now = now
        self._store = StateStore(path, default={})
        self._data = self._load_safe()

    def _load_safe(self) -> dict:
        try:
            data = self._store.load()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _mutate(self, fn) -> None:
        """鎖保護的 read-modify-write；fail-open：寫失敗只影響持久化，不往上拋。"""
        try:
            new = self._store.update(
                lambda current: fn(dict(current) if isinstance(current, dict) else {})
            )
            self._data = new if isinstance(new, dict) else {}
        except Exception:
            # 至少讓這個 instance 自己看得到剛才的變更（下次重啟才會漏）
            try:
                self._data = fn(dict(self._data))
            except Exception:
                pass

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        def _apply(data: dict) -> dict:
            data[key] = value
            return data

        self._mutate(_apply)


def pick_boke_mode(store: ManzaiVarietyStore) -> str:
    """循環推進挑下一個 ボケ 手法，選中即寫回（跨重啟保存）。

    走 index+1 而不是「隨便挑一個不等於上次的」——後者會在前兩種之間乒乓，
    剩下三種永遠輪不到（2026-09-22 實測五次只出現兩種手法）。
    """
    last = store.get(_LAST_MODE_KEY)
    try:
        idx = BOKE_MODES.index(last)
    except ValueError:
        idx = -1  # 沒有紀錄 / 壞資料 → 從第一個開始
    mode = BOKE_MODES[(idx + 1) % len(BOKE_MODES)]
    store.set(_LAST_MODE_KEY, mode)
    return mode


def boke_block(mode: str) -> str:
    """該 mode 的 prompt 片段；未知 mode 落 existential。"""
    return _BOKE_BLOCKS.get(mode, _BOKE_BLOCKS["existential"])


def recent_lines(store: ManzaiVarietyStore, n: int = RING_SIZE) -> list[str]:
    """最近用過的對白文字，新→舊。"""
    lines = store.get(_RECENT_KEY) or []
    if not isinstance(lines, list):
        return []
    return [s for s in lines if isinstance(s, str)][:n]


def record_lines(store: ManzaiVarietyStore, segments) -> None:
    """把這次產出的每段 text 推進 ring buffer，只留最近 RING_SIZE 筆（FIFO）。"""
    texts = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        text = (seg.get("text") or "").strip()
        if text:
            texts.append(text)
    if not texts:
        return

    def _apply(data: dict) -> dict:
        old = data.get(_RECENT_KEY)
        old = [s for s in old if isinstance(s, str)] if isinstance(old, list) else []
        # texts 由舊到新；逐一插到最前面 → 最後一段排在最前
        merged = list(old)
        for text in texts:
            merged.insert(0, text)
        data[_RECENT_KEY] = merged[:RING_SIZE]
        return data

    store._mutate(_apply)


def build_avoid_block(recent) -> str:
    """最近用過的句子 → prompt 禁用清單。沒有東西可避就回空字串。"""
    lines = [s for s in (recent or []) if isinstance(s, str) and s.strip()]
    if not lines:
        return ""
    listed = "\n".join(f"- {s}" for s in lines)
    return (
        "【最近用過的句子——嚴禁重複】\n"
        "以下是最近幾次已經講過的台詞。這次禁止重複它們的開頭或收尾，"
        "也不准換個同義詞硬湊同一個意思，換一個完全不同的切入點：\n"
        f"{listed}"
    )

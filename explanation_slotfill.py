"""explanation_slotfill.py — 推薦解釋層：槽位填空式生成（constrained slot-filling）。

設計動機（`jackhuang-main-design-MusicRecEngine-20260820-114251.md`）：解釋內容
必須根基於可查證的本地資料，不准 LLM 自由編造事實（`feedback_no_llm_invented_facts`）。
這裡用固定句型的具名空格 + 純函式渲染取代自由生成：每個空格的值都直接從
`music_recommender.Evidence` 算出、經型別檢查後才 render，結構上不可能出現
evidence 之外的內容——不需要 LLM 呼叫，也不需要二次驗證/eval suite/shadow 觀察期。

刻意**不衍生自** `dj_story_arc.py` 的 long_tail 消費路徑——那條路徑目的是故事
驚喜、允許 LLM 自由發揮，跟這裡「解釋必須可查證」的目的相反，兩者連呼叫路徑
都不共用（本模組完全不呼叫 LLM）。

多套句型模板輪替（仿 `dj_topic_selector._pick_fallback_mode` 的冷卻/輪替 pattern）
避免「技術上正確但無聊/重複」——同一個 (signal_type, subject) 組合連續兩次
不選同一個模板。
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass

from music_recommender import Evidence

logger = logging.getLogger(__name__)

SECONDS_PER_WEEK = 7 * 86400.0
DEFAULT_TEMPLATE_STATE_PATH = "records/explanation_template_state.json"


@dataclass
class Template:
    text: str
    slots: tuple[str, ...]  # 具名空格；render 前逐一型別檢查，缺一個就不合格


# (signal_type, subject) → 句型清單。subject "you" 才人名化；"you_all" 用「你們」。
_TEMPLATES: dict[tuple[str, str], list[Template]] = {
    ("listen", "you"): [
        Template("你 {weeks_ago} 週前聽過這首，那陣子連續點了 {play_count} 次", ("weeks_ago", "play_count")),
        Template("這首你點過 {play_count} 次，上次是 {weeks_ago} 週前", ("play_count", "weeks_ago")),
        Template("老歌新聽——你 {weeks_ago} 週前就愛過這首了", ("weeks_ago",)),
        Template("這首你點過 {play_count} 次，是你的老朋友了", ("play_count",)),
    ],
    ("listen", "you_all"): [
        Template("你們一起聽過這首，合計點了 {play_count} 次", ("play_count",)),
        Template("這首是你們的共同回憶，{weeks_ago} 週前一起聽過", ("weeks_ago",)),
    ],
    ("like", "you"): [
        Template("你 {weeks_ago} 週前按讚收藏過這首", ("weeks_ago",)),
        Template("這首你之前點讚收藏過", ()),
    ],
    ("like", "you_all"): [
        Template("你們都讚過這首", ()),
    ],
    ("adjacent_artist", "you"): [
        Template("這是你常聽歌手的鄰近推薦，還沒聽過但可能會喜歡", ()),
        Template("跳出你的常聽清單，但風格相鄰，試試看", ()),
    ],
    ("adjacent_artist", "you_all"): [
        Template("這是跳出你們常聽範圍的鄰近推薦，一起試試看", ()),
    ],
    ("radio_related", "you_all"): [
        Template("YouTube Music 常把這首和你們聽過的《{seed_title}》放在同一份歌單", ("seed_title",)),
        Template("跟你們聽過的《{seed_title}》風格相近，YouTube Music 判斷放在一起", ("seed_title",)),
    ],
}


def _compute_slot_values(evidence: Evidence) -> dict[str, object]:
    """把 Evidence 換算成候選槽位值——每格都先型別檢查，檢查不過的槽位直接不進字典
    （渲染時該槽位缺值 → 該模板不合格，不會出現非法值）。
    """
    values: dict[str, object] = {}
    if isinstance(evidence.play_count, int) and not isinstance(evidence.play_count, bool) and evidence.play_count > 0:
        values["play_count"] = evidence.play_count
    if isinstance(evidence.timestamp, (int, float)) and not isinstance(evidence.timestamp, bool):
        diff = time.time() - evidence.timestamp
        if diff >= 0:
            values["weeks_ago"] = int(diff / SECONDS_PER_WEEK)
    if isinstance(evidence.seed_title, str) and evidence.seed_title:
        values["seed_title"] = evidence.seed_title
    return values


def _eligible_templates(key: tuple[str, str], values: dict[str, object]) -> list[Template]:
    return [t for t in _TEMPLATES.get(key, []) if all(s in values for s in t.slots)]


class TemplateRotationStore:
    """避免同一 (signal_type, subject) 組合連續兩次選到同一個模板 index。

    disk JSON 持久化（撓過重啟），fail-open：壞檔/IO 失敗當空狀態，不擋解釋生成。
    """

    def __init__(self, path: str = DEFAULT_TEMPLATE_STATE_PATH):
        self._path = path
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

    def get_last_index(self, key: str) -> int | None:
        return self._data.get(key)

    def set_last_index(self, key: str, index: int) -> None:
        self._data[key] = index
        self._save()


def generate_explanation(evidence: Evidence | None, *, store: TemplateRotationStore) -> str | None:
    """把 Evidence render 成一句解釋。

    無 evidence／該 (signal_type, subject) 沒有合適模板（所需槽位型別檢查沒過）
    → None，caller 該跳過該次解釋顯示，不影響推薦/播放本身。
    """
    if evidence is None:
        return None
    key = (evidence.signal_type, evidence.subject)
    values = _compute_slot_values(evidence)
    templates = _eligible_templates(key, values)
    if not templates:
        logger.info("無合適解釋模板：%s，跳過本次解釋", key)
        return None

    state_key = f"{key[0]}:{key[1]}"
    last_index = store.get_last_index(state_key)
    index = next((i for i in range(len(templates)) if i != last_index), 0)
    store.set_last_index(state_key, index)

    try:
        return templates[index].text.format(**values)
    except (KeyError, ValueError, IndexError) as e:
        logger.warning("解釋模板 render 失敗，跳過本次解釋：%s", e)
        return None

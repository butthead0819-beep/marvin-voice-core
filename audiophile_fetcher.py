"""聽覺放大鏡導聆稿抓取（docs/PLAN_audiophile_music_tour.md Phase 2）。

重用 grounded_answer（free→付費鏈 + PaidUsageGuard 記帳 + L1/L2 幻覺 guard），不自開 client、
不寫死 model。快取在 SongKnowledgeStore 同檔但獨立 key「audiophile::…」——不跟
get_or_extract_insight 的記錄共用 dict（那邊 set 是整份覆寫，共用會互洗欄位）。
失敗回保底台詞且不寫快取。
"""
from __future__ import annotations

import logging
import time

from dj_prompt_builder import build_audiophile_guide_prompt
from intent_agents.grounded_qa_agent import grounded_answer

logger = logging.getLogger(__name__)

# 背景預渲染用，grounded 搜尋+寫 100 字比 AmbientQA 的 8s 慢
GUIDE_TIMEOUT_S = 20.0
FALLBACK_GUIDE_TEMPLATE = "這首〈{title}〉我就不多嘴了，戴好耳機，從第一個音開始聽。"
_KEY_PREFIX = "audiophile::"


def _song_label(title: str, artist: str) -> str:
    return f"{artist} - {title}" if artist else title


async def fetch_audiophile_guide(
    title: str,
    artist: str,
    *,
    free_client,
    paid_client,
    guard,
    store,
) -> str:
    """title/artist → 導聆台詞；快取命中零 API 呼叫，失敗回保底台詞且不寫快取。"""
    label = _song_label(title, artist)
    key = _KEY_PREFIX + label

    cached = (store.get(key) or {}).get("audiophile_guide")
    if cached:
        return cached

    res = None
    try:
        res = await grounded_answer(
            free_client, paid_client, guard, label,
            system_prompt=build_audiophile_guide_prompt(label),
            caller="audiophile_guide",
            timeout=GUIDE_TIMEOUT_S,
        )
    except Exception as e:
        logger.warning(f"[Audiophile] grounded_answer 例外: {e}")

    if res is None:
        logger.info(f"[Audiophile] {label} 查不到可靠資料，回保底台詞")
        return FALLBACK_GUIDE_TEMPLATE.format(title=title)

    text, sources = res
    store.set(key, {"audiophile_guide": text, "sources": sources, "ts": time.time()})
    return text

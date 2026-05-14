from __future__ import annotations
import random
import logging

logger = logging.getLogger(__name__)

# Curated list of concrete, guessable physical objects for Busted themes.
# All items are tangible nouns — no abstract concepts allowed.
CONCRETE_OBJECTS: list[str] = [
    "吉他", "鋼琴", "耳機", "麥克風", "電吉他",
    "搖桿", "鍵盤", "滑鼠", "螢幕", "耳塞",
    "筷子", "湯匙", "菜刀", "砧板", "電鍋",
    "行李箱", "背包", "雨傘", "手錶", "眼鏡",
    "火箭", "望遠鏡", "指南針", "帳篷", "睡袋",
    "珊瑚", "貝殼", "燈塔", "錨", "槳",
    "爆米花", "爆米花機", "電影票", "快門", "底片",
    "球鞋", "護具", "哨子", "跑道", "跳繩",
    "書籤", "放大鏡", "鉛筆", "橡皮擦", "剪刀",
    "黑洞", "隕石", "太空衣", "衛星", "天文台",
    "貓", "狗", "倉鼠", "魚缸", "鳥籠",
    "咖啡機", "茶壺", "馬克杯", "托盤", "瓦斯爐",
    "晶片", "電池", "插頭", "變壓器", "天線",
]

# Fallback used when memory is completely empty
_FALLBACK_TOPIC = "吉他"
_FALLBACK_ANSWER = "吉他"

# Topics that are already concrete nouns can be used directly as the answer.
# Anything shorter than 2 chars is treated as too vague and gets a related suffix.
_TOPIC_ANSWER_MAP: dict[str, str] = {
    "音樂": "耳機",
    "遊戲": "搖桿",
    "食物": "筷子",
    "旅行": "行李箱",
    "科技": "晶片",
    "太空": "火箭",
    "海洋": "珊瑚",
    "電影": "爆米花",
    "運動": "球鞋",
    "書": "書籤",
    "貓": "貓",
    "狗": "狗",
    "咖啡": "咖啡",
    "宇宙": "黑洞",
}


def _derive_answer(topic: str) -> str:
    """
    Derive a concrete noun answer from a topic string.

    Priority:
    1. Direct lookup in _TOPIC_ANSWER_MAP.
    2. If the topic itself is ≥2 chars (already concrete-ish), use it directly.
    3. Otherwise return the fallback answer.
    """
    if topic in _TOPIC_ANSWER_MAP:
        return _TOPIC_ANSWER_MAP[topic]
    if len(topic) >= 2:
        return topic
    return _FALLBACK_ANSWER


def pick_topic_and_answer(memory_manager) -> tuple[str, str]:
    """
    Uses suki_memory to find a recent concrete topic, then returns (topic, answer).
    answer = a concrete noun derived from the topic (e.g. topic="音樂" → answer="耳機").

    Strategy:
    1. Call memory_manager.get_proactive_topics() → list of topic strings.
    2. Also check all players' emotional_highlights and behavioral_patterns.
    3. Pick one topic randomly from the combined pool (deduplicated).
    4. Derive a concrete noun answer from the topic using simple heuristics.
       (topic IS the answer if it's already a concrete noun ≥2 chars, else use map or fallback.)
    5. Fallback: return ("宇宙", "黑洞") if memory is empty.

    Returns (topic_description, answer) tuple.
    """
    pool: list[str] = []

    # --- Step 1: proactive topics from memory manager ---
    try:
        proactive = memory_manager.get_proactive_topics()
        if isinstance(proactive, list):
            pool.extend(t for t in proactive if isinstance(t, str) and t.strip())
    except Exception as e:
        logger.warning(f"[TopicPicker] get_proactive_topics failed: {e}")

    # --- Step 2: scan all player emotional_highlights and behavioral_patterns ---
    try:
        # MemoryManager exposes _cache as {username: player_dict}
        cache: dict = getattr(memory_manager, "_cache", {})
        for username, player in cache.items():
            if not isinstance(player, dict):
                continue

            # emotional_highlights: list of {"moment": str, "valence": str, ...}
            for highlight in player.get("emotional_highlights", []):
                if isinstance(highlight, dict):
                    moment = highlight.get("moment", "")
                    if isinstance(moment, str) and moment.strip():
                        # Use the first meaningful word/phrase from the moment
                        words = moment.strip().split()
                        if words:
                            pool.append(words[0])

            # behavioral_patterns: dict of {key: value}
            for key, val in player.get("behavioral_patterns", {}).items():
                if isinstance(key, str) and key.strip():
                    pool.append(key)
                if isinstance(val, str) and val.strip():
                    pool.append(val)
    except Exception as e:
        logger.warning(f"[TopicPicker] scanning player cache failed: {e}")

    # --- Step 3: deduplicate, filter, and pick ---
    unique_pool = list({t.strip() for t in pool if t.strip()})
    if not unique_pool:
        logger.debug("[TopicPicker] memory empty, using fallback topic")
        return (_FALLBACK_TOPIC, _FALLBACK_ANSWER)

    topic = random.choice(unique_pool)

    # --- Step 4: derive concrete answer ---
    answer = _derive_answer(topic)

    logger.debug(f"[TopicPicker] picked topic={topic!r} → answer={answer!r}")
    return (topic, answer)


def pick(memory_manager) -> tuple[str, str]:
    """Alias for pick_topic_and_answer."""
    return pick_topic_and_answer(memory_manager)


def pick_theme_candidates(memory_manager, n: int = 3) -> list[str]:
    """Return n distinct concrete physical objects from CONCRETE_OBJECTS.
    Always draws from the curated list so themes are never abstract.
    """
    pool = list(CONCRETE_OBJECTS)
    random.shuffle(pool)
    return pool[:n]

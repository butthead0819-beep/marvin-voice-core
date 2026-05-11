from __future__ import annotations
import random
import logging

logger = logging.getLogger(__name__)

# Fallback used when memory is completely empty
_FALLBACK_TOPIC = "宇宙"
_FALLBACK_ANSWER = "黑洞"

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
    """Return up to n distinct theme strings drawn from chat memory.
    Falls back to built-in topics when memory is thin.
    """
    pool: list[str] = []

    try:
        proactive = memory_manager.get_proactive_topics()
        if isinstance(proactive, list):
            pool.extend(t for t in proactive if isinstance(t, str) and t.strip())
    except Exception:
        pass

    try:
        cache: dict = getattr(memory_manager, "_cache", {})
        for player in cache.values():
            if not isinstance(player, dict):
                continue
            for highlight in player.get("emotional_highlights", []):
                if isinstance(highlight, dict):
                    moment = highlight.get("moment", "")
                    if isinstance(moment, str) and moment.strip():
                        pool.append(moment.strip().split()[0])
            for key, val in player.get("behavioral_patterns", {}).items():
                if isinstance(key, str) and key.strip():
                    pool.append(key)
                if isinstance(val, str) and val.strip():
                    pool.append(val)
    except Exception:
        pass

    unique = list({t.strip() for t in pool if len(t.strip()) >= 2})
    if len(unique) < n:
        # Pad with built-in topics so we always have n choices
        fallbacks = [t for t in _TOPIC_ANSWER_MAP if t not in unique]
        random.shuffle(fallbacks)
        unique.extend(fallbacks)

    random.shuffle(unique)
    return unique[:n]

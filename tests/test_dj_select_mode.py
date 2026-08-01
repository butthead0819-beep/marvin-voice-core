"""TDD — dj_topic_selector.select_mode：本地決定串場 mode，取代讓 LLM 自己判斷
「有沒有話題、要不要硬掰、事件主角在不在場」。

三個行為：
  1. 生活素材的主角現在不在場 → 直接跳過，換下一個候選（掛名/代名詞護欄）。
  2. life/interest 都沒有時，本地在 conversation/prev_song/quick 間輪替，
     不會每次都落在同一種 fallback。
  3. fallback 輪替狀態跨 store 實例持久化（跟話題冷卻一樣走 disk）。
"""
from __future__ import annotations

from dj_life_context import LifeCore
from dj_topic_selector import TopicCooldownStore, select_mode


def _store(tmp_path):
    return TopicCooldownStore(str(tmp_path / "c.json"))


# ── 1. 主角不在場的生活素材被跳過 ────────────────────────────────────────────

def test_life_core_skipped_when_actor_absent(tmp_path):
    store = _store(tmp_path)
    life = [LifeCore("showay 去台南練焊接", speakers=("showay",))]
    topic, mode = select_mode(life, [], store, present_members={"狗與露"})
    assert mode != "life"


def test_life_core_used_when_actor_present(tmp_path):
    store = _store(tmp_path)
    life = [LifeCore("showay 去台南練焊接", speakers=("showay",))]
    topic, mode = select_mode(life, [], store, present_members={"showay", "狗與露"})
    assert (topic, mode) == ("showay 去台南練焊接", "life")


def test_life_core_without_named_actor_always_used(tmp_path):
    """沒有特定主角（speakers 空）的公開話題，不受在場過濾影響。"""
    store = _store(tmp_path)
    life = [LifeCore("最近天氣很怪")]
    topic, mode = select_mode(life, [], store, present_members={"任何人"})
    assert (topic, mode) == ("最近天氣很怪", "life")


def test_present_members_none_does_not_filter():
    """vc 不可用（present_members=None）→ fail-open，不過濾。"""
    import tempfile
    store = TopicCooldownStore(tempfile.mktemp(suffix=".json"))
    life = [LifeCore("showay 去台南練焊接", speakers=("showay",))]
    topic, mode = select_mode(life, [], store, present_members=None)
    assert mode == "life"


def test_falls_through_to_next_candidate_when_actor_absent(tmp_path):
    store = _store(tmp_path)
    life = [
        LifeCore("showay 去台南練焊接", speakers=("showay",)),
        LifeCore("大肚在準備搬家", speakers=("大肚",)),
    ]
    topic, mode = select_mode(life, [], store, present_members={"大肚"})
    assert (topic, mode) == ("大肚在準備搬家", "life")


# ── 2. 沒話題時本地輪替 fallback ─────────────────────────────────────────────

def test_fallback_rotates_not_always_quick(tmp_path):
    store = _store(tmp_path)
    _, mode1 = select_mode([], [], store, has_conversation=True, has_prev_song=True)
    _, mode2 = select_mode([], [], store, has_conversation=True, has_prev_song=True)
    assert mode1 != mode2, "連續兩次不該落在同一種 fallback"


def test_fallback_skips_unavailable_candidates(tmp_path):
    """沒有對話、沒有上一首 → 只剩 atmosphere/quick 輪替（都不需要 has_* 素材）。"""
    store = _store(tmp_path)
    _, mode = select_mode([], [], store, has_conversation=False, has_prev_song=False)
    assert mode in ("atmosphere", "quick")


def test_fresh_store_picks_atmosphere_before_quick(tmp_path):
    """全新 store（沒有輪替紀錄）→ atmosphere 排在 quick 前面，第一次選它。"""
    store = _store(tmp_path)
    _, mode = select_mode([], [], store, has_conversation=False, has_prev_song=False)
    assert mode == "atmosphere"


def test_fallback_prefers_available_over_last_used(tmp_path):
    store = _store(tmp_path)
    store.set_last_fallback("conversation")
    _, mode = select_mode([], [], store, has_conversation=True, has_prev_song=False)
    # 上次用過 conversation → 换下一個可用的（atmosphere 永遠可用，排在 conversation 後面）
    assert mode == "atmosphere"


def test_fallback_state_persists_across_store_instances(tmp_path):
    path = str(tmp_path / "c.json")
    store1 = TopicCooldownStore(path)
    _, mode1 = select_mode([], [], store1, has_conversation=True, has_prev_song=True)
    store2 = TopicCooldownStore(path)  # 模擬重啟
    _, mode2 = select_mode([], [], store2, has_conversation=True, has_prev_song=True)
    assert mode2 != mode1


# ── 3. life/interest 優先於 fallback ────────────────────────────────────────

def test_life_topic_wins_over_fallback(tmp_path):
    store = _store(tmp_path)
    topic, mode = select_mode(
        ["昨天去爬山"], [], store, has_conversation=True, has_prev_song=True,
    )
    assert (topic, mode) == ("昨天去爬山", "life")


def test_plain_str_and_tuple_life_items_still_work(tmp_path):
    """相容舊格式（純 str / (text, meme_id)），不需要是 LifeCore。"""
    store = _store(tmp_path)
    topic, mode = select_mode(["昨天去爬山"], [], store)
    assert (topic, mode) == ("昨天去爬山", "life")

"""TDD — dj_topic_selector.select_mode：本地扭蛋池決定串場 mode，取代讓 LLM 自己判斷
「有沒有話題、要不要硬掰、事件主角在不在場」。

行為：
  1. 生活素材的主角現在不在場 → 直接跳過，換下一個候選（掛名/代名詞護欄）。
  2. 沒有素材的 mode 永遠不進扭蛋池（資格門檻）。
  3. 池大小 >1 時，相鄰兩次抽選不會落在同一個 mode（不連抽）。
  4. 不連抽狀態跨 store 實例持久化（跟話題冷卻一樣走 disk）。

以下測試用 monkeypatch 固定 MODE_WEIGHTS，讓「素材存在時會被抽中」的資格語意在扭蛋
隨機性下仍是決定性的斷言——測的是「進不進池」，不是「優先序」（優先序已隨扭蛋池拔除）。
"""
from __future__ import annotations

import dj_topic_selector
from dj_life_context import LifeCore
from dj_topic_selector import TopicCooldownStore, select_mode


def _store(tmp_path):
    return TopicCooldownStore(str(tmp_path / "c.json"))


def _only(monkeypatch, *modes):
    """monkeypatch MODE_WEIGHTS，讓扭蛋池只可能抽中 modes 裡列的那些（其餘權重 0）。"""
    weights = {m: 1.0 for m in modes}
    monkeypatch.setattr(dj_topic_selector, "MODE_WEIGHTS", weights)


# ── 1. 主角不在場的生活素材被跳過 ────────────────────────────────────────────

def test_life_core_skipped_when_actor_absent(tmp_path):
    store = _store(tmp_path)
    life = [LifeCore("showay 去台南練焊接", speakers=("showay",))]
    topic, mode = select_mode(life, [], store, present_members={"狗與露"})
    assert mode != "life"


def test_life_core_used_when_actor_present(tmp_path, monkeypatch):
    _only(monkeypatch, "life")
    store = _store(tmp_path)
    life = [LifeCore("showay 去台南練焊接", speakers=("showay",))]
    topic, mode = select_mode(life, [], store, present_members={"showay", "狗與露"})
    assert (topic, mode) == ("showay 去台南練焊接", "life")


def test_life_core_without_named_actor_always_used(tmp_path, monkeypatch):
    """沒有特定主角（speakers 空）的公開話題，不受在場過濾影響。"""
    _only(monkeypatch, "life")
    store = _store(tmp_path)
    life = [LifeCore("最近天氣很怪")]
    topic, mode = select_mode(life, [], store, present_members={"任何人"})
    assert (topic, mode) == ("最近天氣很怪", "life")


def test_present_members_none_does_not_filter(monkeypatch):
    """vc 不可用（present_members=None）→ fail-open，不過濾。"""
    import tempfile
    _only(monkeypatch, "life")
    store = TopicCooldownStore(tempfile.mktemp(suffix=".json"))
    life = [LifeCore("showay 去台南練焊接", speakers=("showay",))]
    topic, mode = select_mode(life, [], store, present_members=None)
    assert mode == "life"


def test_falls_through_to_next_candidate_when_actor_absent(tmp_path, monkeypatch):
    _only(monkeypatch, "life")
    store = _store(tmp_path)
    life = [
        LifeCore("showay 去台南練焊接", speakers=("showay",)),
        LifeCore("大肚在準備搬家", speakers=("大肚",)),
    ]
    topic, mode = select_mode(life, [], store, present_members={"大肚"})
    assert (topic, mode) == ("大肚在準備搬家", "life")


# ── 2. 沒素材的 mode 永不進池 ────────────────────────────────────────────────

def test_fallback_skips_unavailable_candidates(tmp_path):
    """沒有對話、沒有上一首 → 只剩 atmosphere/quick 可抽。"""
    store = _store(tmp_path)
    _, mode = select_mode([], [], store, has_conversation=False, has_prev_song=False)
    assert mode in ("atmosphere", "quick")


def test_no_emotional_highlights_still_falls_to_fallback_rotation(tmp_path):
    store = _store(tmp_path)
    _, mode = select_mode(
        [], [], store, has_conversation=False, has_prev_song=False,
        emotional_highlights=[],
    )
    assert mode in ("atmosphere", "quick")


def test_fallback_state_persists_across_store_instances(tmp_path):
    """不連抽狀態跨 store 實例（模擬重啟）持久化：池大小 >1 時，第二次一定跟第一次不同。"""
    path = str(tmp_path / "c.json")
    store1 = TopicCooldownStore(path)
    _, mode1 = select_mode([], [], store1, has_conversation=True, has_prev_song=True)
    store2 = TopicCooldownStore(path)  # 模擬重啟
    _, mode2 = select_mode([], [], store2, has_conversation=True, has_prev_song=True)
    assert mode2 != mode1


# ── 3. life/interest 等話題類存在時進池 ──────────────────────────────────────

def test_plain_str_and_tuple_life_items_still_work(tmp_path, monkeypatch):
    """相容舊格式（純 str / (text, meme_id)），不需要是 LifeCore。"""
    _only(monkeypatch, "life")
    store = _store(tmp_path)
    topic, mode = select_mode(["昨天去爬山"], [], store)
    assert (topic, mode) == ("昨天去爬山", "life")


# ── 4. emotional_highlight：有素材時進池 ─────────────────────────────────────

def test_emotional_highlight_used_when_no_life_or_interest(tmp_path, monkeypatch):
    _only(monkeypatch, "emotional_highlight")
    store = _store(tmp_path)
    topic, mode = select_mode(
        [], [], store, emotional_highlights=["上次你說覺得被理解那句話"],
    )
    assert (topic, mode) == ("上次你說覺得被理解那句話", "emotional_highlight")


# ── 5. guide（歌曲卡長版導聆）：只在 has_guide=True 時進池 ───────────────────

def test_guide_never_picked_when_has_guide_false():
    """has_guide 預設 False → guide 不進候選，行為跟舊版完全一致。"""
    import tempfile
    from dj_topic_selector import TopicCooldownStore

    store = TopicCooldownStore(tempfile.mktemp(suffix=".json"))
    for _ in range(6):
        _, mode = select_mode([], [], store, has_conversation=True, has_prev_song=True)
        assert mode != "guide"

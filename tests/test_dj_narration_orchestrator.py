"""Characterization tests — dj_narration_orchestrator（Phase A）。

驗證新 orchestrator 的輸出跟「直接呼叫底層模組的舊寫法」（照抄
cogs/music_cog_tail_dj.py::_run_tail_dj 與
cogs/music_cog_dj_lyrics.py::_fetch_dj_interjection_raw 裡對應那幾行）
產出完全一致——證明 Phase A 只是把呼叫順序包一層，沒有改變行為。
"""
from __future__ import annotations

import dj_topic_selector
from dj_narration_orchestrator import compute_tail_fire_delay, select_narration_mode
from dj_tail_schedule import tail_dj_fire_delay
from dj_topic_selector import TopicCooldownStore, select_mode


def _only(monkeypatch, *modes):
    """monkeypatch MODE_WEIGHTS，讓扭蛋池只可能抽中 modes 裡列的那些（其餘權重 0）——
    這些 characterization test 比較「new vs 照抄舊寫法」兩條路徑的輸出，兩條路徑其實
    呼叫的是同一個（已改成扭蛋池的）select_mode，天然隨機、兩次呼叫不保證一樣；固定
    權重讓兩條路徑都收斂到同一個 mode，才能公平比較。"""
    monkeypatch.setattr(dj_topic_selector, "MODE_WEIGHTS", {m: 1.0 for m in modes})


# ── compute_tail_fire_delay vs. _run_tail_dj 舊寫法 ──────────────────────────

def _legacy_tail_fire_delay(duration_s, elapsed_s, *, highlight_start_s=None, lead_s=8.0):
    """逐行照抄 _run_tail_dj 的舊寫法（不經 orchestrator）。"""
    if not duration_s:
        return None
    if highlight_start_s:
        duration_s = max(0.0, duration_s - highlight_start_s)
    return tail_dj_fire_delay(duration_s, elapsed_s, lead_s=lead_s)


def test_tail_fire_delay_matches_legacy_across_cases():
    cases = [
        dict(duration_s=None, elapsed_s=0.0),
        dict(duration_s=0, elapsed_s=0.0),
        dict(duration_s=200.0, elapsed_s=10.0),
        dict(duration_s=200.0, elapsed_s=10.0, highlight_start_s=30.0),
        dict(duration_s=20.0, elapsed_s=1.0),  # 太短的歌
        dict(duration_s=200.0, elapsed_s=195.0),  # 已過點火窗
        dict(duration_s=200.0, elapsed_s=10.0, lead_s=5.0),
    ]
    for kwargs in cases:
        assert compute_tail_fire_delay(**kwargs) == _legacy_tail_fire_delay(**kwargs), kwargs


def test_tail_fire_delay_none_when_duration_unknown():
    assert compute_tail_fire_delay(None, 5.0) is None
    assert compute_tail_fire_delay(0, 5.0) is None


def test_tail_fire_delay_accounts_for_highlight_start_offset():
    # highlight_start_s 讓有效 duration 變短，點火時間點也跟著提前。
    without = compute_tail_fire_delay(200.0, 10.0)
    with_highlight = compute_tail_fire_delay(200.0, 10.0, highlight_start_s=30.0)
    assert with_highlight is not None and without is not None
    assert with_highlight < without


# ── select_narration_mode vs. _fetch_dj_interjection_raw 舊寫法 ─────────────

def _legacy_select_mode(
    life, interests, store, *,
    present_members=None, has_conversation=False, has_prev_song=False,
    emotional_highlights=None, news_items=None, autopilot_reason="",
):
    """逐行照抄 _fetch_dj_interjection_raw 裡 select_mode + reason 覆蓋那段。"""
    topic, mode = select_mode(
        life, interests, store,
        present_members=present_members,
        has_conversation=has_conversation,
        has_prev_song=has_prev_song,
        emotional_highlights=emotional_highlights,
        news_items=news_items,
    )
    if autopilot_reason and mode in ("quick", "atmosphere"):
        mode = "reason"
    return topic, mode


def _fresh_store(tmp_path, name: str) -> TopicCooldownStore:
    # 每個 store 用獨立路徑/初始狀態，避免 select_mode 的冷卻/輪替 side effect
    # 跨兩條路徑互相污染，才能公平比較「同樣輸入、兩條路徑」的輸出。
    return TopicCooldownStore(str(tmp_path / name), now=lambda: 1000.0)


def test_select_narration_mode_matches_legacy_when_life_available(tmp_path, monkeypatch):
    _only(monkeypatch, "life")
    kwargs = dict(life=["昨天去爬山"], interests=["喜歡周杰倫"])
    new = select_narration_mode(topic_store=_fresh_store(tmp_path, "a.json"), **kwargs)
    old = _legacy_select_mode(kwargs["life"], kwargs["interests"], _fresh_store(tmp_path, "b.json"))
    assert new == old == ("昨天去爬山", "life")


def test_select_narration_mode_matches_legacy_fallback_rotation(tmp_path, monkeypatch):
    _only(monkeypatch, "prev_song")
    kwargs = dict(
        life=[], interests=[], has_conversation=True, has_prev_song=True,
    )
    new = select_narration_mode(topic_store=_fresh_store(tmp_path, "a.json"), **kwargs)
    old = _legacy_select_mode(
        kwargs["life"], kwargs["interests"], _fresh_store(tmp_path, "b.json"),
        has_conversation=kwargs["has_conversation"], has_prev_song=kwargs["has_prev_song"],
    )
    assert new == old


def test_select_narration_mode_autopilot_reason_overrides_quick(tmp_path, monkeypatch):
    # life/interest/highlight/news 全空、has_conversation/has_prev_song 全 False
    # → 池只剩 atmosphere/quick，固定權重讓 select_mode 必落在 quick，autopilot_reason
    # 存在時 orchestrator 該把它蓋成 "reason"，跟舊寫法一致。
    _only(monkeypatch, "quick")
    kwargs = dict(life=[], interests=[], autopilot_reason="照你的口味挖出來的新歌")
    new = select_narration_mode(topic_store=_fresh_store(tmp_path, "a.json"), **kwargs)
    old = _legacy_select_mode(
        kwargs["life"], kwargs["interests"], _fresh_store(tmp_path, "b.json"),
        autopilot_reason=kwargs["autopilot_reason"],
    )
    assert new == old
    assert new[1] == "reason"


def test_select_narration_mode_has_guide_picks_guide_over_fallback_rotation(tmp_path, monkeypatch):
    # 有歌曲卡導聆可講（has_guide=True）→ 固定權重讓扭蛋池必抽中 guide。
    _only(monkeypatch, "guide")
    kwargs = dict(
        life=[], interests=[], has_conversation=True, has_prev_song=True, has_guide=True,
    )
    topic, mode = select_narration_mode(topic_store=_fresh_store(tmp_path, "a.json"), **kwargs)
    assert mode == "guide"
    assert topic is None  # guide 沒有具體文字素材，caller 自己從 card 讀


def test_select_narration_mode_has_guide_false_never_returns_guide(tmp_path):
    # has_guide 預設 False（未傳）→ guide 不該出現，跟舊版行為完全一致。
    store = _fresh_store(tmp_path, "a.json")
    for _ in range(6):
        _, mode = select_narration_mode(
            life=[], interests=[], topic_store=store,
            has_conversation=True, has_prev_song=True,
        )
        assert mode != "guide"


def test_select_narration_mode_autopilot_reason_does_not_override_guide(tmp_path, monkeypatch):
    # autopilot_reason 只覆蓋 quick/atmosphere，不該搶走 guide（有查證過的真實資料
    # 時優先權比「Marvin 自己編的推薦理由」高）。固定權重讓扭蛋池必抽中 guide。
    _only(monkeypatch, "guide")
    store = _fresh_store(tmp_path, "a.json")
    topic, mode = select_narration_mode(
        life=[], interests=[], topic_store=store,
        has_conversation=True, has_prev_song=True, has_guide=True,
        autopilot_reason="照你的口味挖出來的新歌",
    )
    assert mode == "guide"


def test_select_narration_mode_autopilot_reason_does_not_override_life(tmp_path, monkeypatch):
    # 有具體素材（life）時，autopilot_reason 不該蓋掉它——只搶 quick/atmosphere。
    # 固定權重讓扭蛋池必抽中 life。
    _only(monkeypatch, "life")
    store = _fresh_store(tmp_path, "c.json")
    topic, mode = select_narration_mode(
        life=["昨天去爬山"], interests=[], topic_store=store,
        autopilot_reason="照你的口味挖出來的新歌",
    )
    assert mode == "life"
    assert topic == "昨天去爬山"

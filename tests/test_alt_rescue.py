"""TDD: AltRescue Stage 2 直餵版 — fastpath miss 後掃 STT 備選救糊字點歌。

Gate 判準（2026-07-02，306 筆）：50.7% 有 ≥4 漢字長備選 → 逐句直餵
現有 match()，不蓋 lattice 引擎。設計守門（AltLatticeRescue design v2）：

  G1 意圖前置閘：stripped 須含真點歌前綴（strip_command_prefix 有剝到東西）
  G2 歌單片語不搶單（我的歌單 → PersonalShuffleAgent，2026-06-30 劫走事故）
  G3 side-channel 驗證：strip_wake(slot.raw_text)==stripped，不一致＝lattice
     不屬於這句 → 放棄
  G4 備選命中門檻比 top-1 嚴（score ≥85；top-1 是 80）
  G5 kill-switch MARVIN_ALT_RESCUE 預設 0
"""
from __future__ import annotations

import pytest

from alt_rescue import try_alt_rescue, rescue_mode


class _StubFP:
    """可程式化的 MusicFastPath 替身：hits = {query: (canonical, score, vid)}。"""
    def __init__(self, hits: dict):
        self._hits = hits
        self.calls: list[str] = []

    def match(self, query: str):
        self.calls.append(query)
        return self._hits.get(query)


def _strip_wake(text: str) -> str:
    return text.replace("馬文", "").strip()


def _slot(raw="馬文播放周杰倫的青天", segs=None, ts=0.0):
    # 備選是句級碎片（含前綴），≥4 漢字過濾以 raw 備選計（同 gate 統計口徑）
    return (raw, segs if segs is not None else [["播放周杰倫的晴天"]], ts)


# ── 主案例：miss → rescue 命中 ───────────────────────────────────────────────

def test_rescue_hits_when_alternative_matches():
    # 候選餵 match 前會剝喚醒詞+點歌前綴：播放周杰倫的晴天 → 周杰倫的晴天
    fp = _StubFP({"周杰倫的晴天": ("周杰倫 晴天", 92.0, "vid123")})
    result, reason = try_alt_rescue(
        fp, "播放周杰倫的青天", _slot(), strip_wake_fn=_strip_wake)
    assert result is not None
    assert result["canonical"] == "周杰倫 晴天"
    assert result["video_id"] == "vid123"
    assert result["alt"] == "周杰倫的晴天"
    assert reason == "hit"


def test_rescue_picks_best_scoring_alternative():
    fp = _StubFP({
        "周杰倫的晴天": ("周杰倫 晴天", 88.0, "v1"),
        "蘇打綠小情歌": ("蘇打綠 小情歌", 91.0, "v2"),
    })
    slot = _slot(segs=[["播放周杰倫的晴天", "播放蘇打綠小情歌"]])
    result, _ = try_alt_rescue(
        fp, "播放周杰倫的青天", slot, strip_wake_fn=_strip_wake)
    assert result["canonical"] == "蘇打綠 小情歌"   # 91 > 88


# ── rescue 也 miss → fall through ────────────────────────────────────────────

def test_rescue_misses_returns_none_with_reason():
    fp = _StubFP({})
    result, reason = try_alt_rescue(
        fp, "播放周杰倫的青天", _slot(), strip_wake_fn=_strip_wake)
    assert result is None
    assert reason == "no_alt_hit"


# ── G4 嚴門檻：85 以下不收 ───────────────────────────────────────────────────

def test_rescue_rejects_below_strict_threshold():
    fp = _StubFP({"晴天": ("周杰倫 晴天", 82.0, "v1")})   # top-1 門檻 80 會收，rescue 不收
    result, reason = try_alt_rescue(
        fp, "播放周杰倫的青天", _slot(), strip_wake_fn=_strip_wake)
    assert result is None
    assert reason == "no_alt_hit"


# ── G1 意圖前置閘 ────────────────────────────────────────────────────────────

def test_control_command_not_hijacked():
    """糊字控制指令（下一手）無點歌前綴 → 不進 rescue，留給 command_fastpath。"""
    fp = _StubFP({"下一手": ("周杰倫 擱淺", 99.0, "v9")})
    result, reason = try_alt_rescue(
        fp, "下一手", ("馬文下一手", [["下一手"]], 0.0), strip_wake_fn=_strip_wake)
    assert result is None
    assert reason == "no_music_prefix"
    assert fp.calls == []           # 連 match 都不該碰


def test_chatter_blocked_by_prefix_gate():
    fp = _StubFP({"肉圓": ("五月天 溫柔", 95.0, "v3")})
    result, reason = try_alt_rescue(
        fp, "我今天吃了超好吃的肉圓", ("馬文我今天吃了超好吃的肉圓", [["肉圓好吃"]], 0.0),
        strip_wake_fn=_strip_wake)
    assert result is None
    assert reason == "no_music_prefix"


# ── G2 歌單片語不搶單 ────────────────────────────────────────────────────────

def test_playlist_phrase_not_hijacked():
    fp = _StubFP({"我的歌單": ("茄子蛋 浪流連", 96.0, "v4")})
    result, reason = try_alt_rescue(
        fp, "放我的歌單", ("馬文放我的歌單", [["我的歌單裡的歌"]], 0.0),
        strip_wake_fn=_strip_wake)
    assert result is None
    assert reason == "playlist_phrase"


# ── G3 side-channel 驗證 ─────────────────────────────────────────────────────

def test_slot_mismatch_abandons_rescue():
    """slot 是舊句的 lattice → 不得誤掛到新 query。"""
    fp = _StubFP({"晴天": ("周杰倫 晴天", 92.0, "v1")})
    stale = ("馬文上一句講別的", [["晴天"]], 0.0)
    result, reason = try_alt_rescue(
        fp, "播放周杰倫的青天", stale, strip_wake_fn=_strip_wake)
    assert result is None
    assert reason == "slot_mismatch"


def test_missing_slot_abandons_rescue():
    fp = _StubFP({})
    result, reason = try_alt_rescue(
        fp, "播放周杰倫的青天", None, strip_wake_fn=_strip_wake)
    assert result is None
    assert reason == "no_slot"


def test_bare_command_word_alt_not_fed_to_match():
    """「馬文播放」剝完只剩裸「播放」→ 不得餵 match——2 音節垃圾 token 會被
    長歌名 token_set 全覆蓋假 100 分（2026-07-02 離線重放實證：播放→海波浪 100）。"""
    fp = _StubFP({"播放": ("方瑞娥 海波浪", 100.0, "vX")})
    slot = ("馬文播放周杰倫的青天", [["馬文播放"]], 0.0)
    result, reason = try_alt_rescue(
        fp, "播放周杰倫的青天", slot, strip_wake_fn=_strip_wake)
    assert result is None
    assert reason == "no_alt_hit"
    assert fp.calls == []           # 裸指令詞連 match 都不碰


def test_short_alternatives_skipped():
    """<4 漢字備選（gate 統計的碎片類）不餵——雜訊假命中面。"""
    fp = _StubFP({"晴": ("周杰倫 晴天", 90.0, "v1")})
    slot = _slot(segs=[["晴", "青", "天"]])
    result, reason = try_alt_rescue(
        fp, "播放周杰倫的青天", slot, strip_wake_fn=_strip_wake)
    assert result is None
    assert reason == "no_alt_hit"
    assert fp.calls == []


# ── G5 kill-switch ───────────────────────────────────────────────────────────

def test_rescue_mode_defaults_off(monkeypatch):
    monkeypatch.delenv("MARVIN_ALT_RESCUE", raising=False)
    assert rescue_mode() == "0"


@pytest.mark.parametrize("val,expected", [
    ("0", "0"), ("shadow", "shadow"), ("on", "on"), ("weird", "0"),
])
def test_rescue_mode_parses_env(monkeypatch, val, expected):
    monkeypatch.setenv("MARVIN_ALT_RESCUE", val)
    assert rescue_mode() == expected

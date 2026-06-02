"""ack_templates registry — 一份宣告驅動所有 ack 的渲染與播放。

統一前散落 5 支 render 腳本 + 3 個 _play_*ack* 分支；統一後：
- 加新 ack = registry 加一條，零新程式碼
- pool（要渲染的音檔）與 category（播放政策）分離：assets/acks 同一 pool
  被 wake / nemoclaw / filler 三種 category 共用，但播放政策各異
"""
from __future__ import annotations


import ack_templates as A


# ── Voice ───────────────────────────────────────────────────────────────────

def test_voices_match_suki_marvin_params():
    """渲染聲音須與 SukiTTS 預設一致（zh 厭世馬文 / en）。"""
    zh = A.VOICES["marvin_zh"]
    assert (zh.voice, zh.rate, zh.pitch) == ("zh-TW-YunJheNeural", "-20%", "-15Hz")
    assert A.VOICES["marvin_en"].voice == "en-GB-RyanNeural"


# ── Pools ───────────────────────────────────────────────────────────────────

def test_all_pools_have_unique_dirs():
    dirs = [p.directory for p in A.POOLS.values()]
    assert len(dirs) == len(set(dirs))


def test_pool_voice_keys_exist():
    for p in A.POOLS.values():
        assert p.voice_key in A.VOICES


def test_status_pool_has_16_items():
    """4 狀態 × 2 tier × 2 變體。"""
    status = A.POOLS["status"]
    assert len(status.items) == 16
    # 檔名前綴格式 {state}_{tier}_{i}
    names = {fn for _, fn in status.items}
    assert "thinking_first_1.mp3" in names
    assert "fallback_second_2.mp3" in names


def test_wake_pool_keeps_legacy_filenames():
    """既有檔名不可變（gitignored 檔已存在，skip-existing 才不重渲）。"""
    names = {fn for _, fn in A.POOLS["wake_zh"].items}
    assert "ack_1.mp3" in names
    names_en = {fn for _, fn in A.POOLS["wake_en"].items}
    assert "ack_en_1.mp3" in names_en
    music = {fn for _, fn in A.POOLS["music"].items}
    assert "music_ack_01.mp3" in music
    assert {fn for _, fn in A.POOLS["music_fail"].items} == {"music_fail.mp3"}


# ── Categories（播放政策）────────────────────────────────────────────────────

def test_wake_category_policy():
    c = A.CATEGORIES["wake"]
    assert c.urgent is True          # 音樂中走熱切換
    assert c.prewarm_tts is True     # ack 預告 Marvin 回應 → 暖 TTS
    assert c.text_fallback           # 連檔都沒時即時合成
    # lang 分流
    assert A.pool_for("wake", lang="zh").key == "wake_zh"
    assert A.pool_for("wake", lang="en").key == "wake_en"


def test_music_category_falls_back_to_wake_pool_when_empty():
    c = A.CATEGORIES["music"]
    assert c.urgent is True
    assert c.empty_fallback_pool == "wake_zh"
    assert c.wait_if_busy > 0


def test_music_fail_not_urgent():
    c = A.CATEGORIES["music_fail"]
    assert c.urgent is False
    assert c.empty_fallback_pool == "wake_zh"


def test_nemoclaw_uses_lock_no_hotswap():
    c = A.CATEGORIES["nemoclaw"]
    assert c.urgent is False
    assert c.use_lock is True
    assert c.prewarm_tts is False
    assert A.pool_for("nemoclaw", lang="en").key == "wake_en"


def test_status_category_variant_glob():
    c = A.CATEGORIES["status"]
    assert c.urgent is True
    assert c.variant_glob is True     # 檔名前綴 = variant（{state}_{tier}）


def test_filler_barges_in_without_lock():
    c = A.CATEGORIES["filler"]
    assert c.use_lock is False        # 故意不鎖，插隊遮蔽延遲
    assert c.urgent is False
    assert c.skip_if_busy is True     # 僅空檔插隊（播放中跳過）


def test_wake_waits_then_awaits_completion():
    c = A.CATEGORIES["wake"]
    assert c.skip_if_busy is False    # 不跳過，等空檔
    assert c.wait_if_busy == 4.0
    assert c.await_completion is True


def test_lock_categories_skip_when_busy():
    for key in ("nemoclaw", "status"):
        c = A.CATEGORIES[key]
        assert c.use_lock is True
        assert c.skip_if_busy is True
        assert c.await_completion is False


def test_every_category_resolves_to_existing_pool():
    for key in A.CATEGORIES:
        for lang in ("zh", "en"):
            assert A.pool_for(key, lang=lang).key in A.POOLS


# ── glob helper ─────────────────────────────────────────────────────────────

def test_glob_pattern_plain_pool():
    pat = A.glob_pattern("wake", lang="zh")
    assert pat == "assets/acks/*.mp3"


def test_glob_pattern_variant():
    pat = A.glob_pattern("status", variant="searching_first")
    assert pat == "assets/acks_status/searching_first_*.mp3"

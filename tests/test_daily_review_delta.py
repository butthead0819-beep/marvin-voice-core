"""Daily review 減量（2026-10-23）：精簡記憶輸入＋只輸出變動＋整天壓縮逐字稿。

背景見 analyze_daily_log.py SYSTEM_PROMPT 的改版：模型不再重寫整份玩家 schema，
只輸出今天新增的變動（new_likes 等），送進去的現有記憶與逐字稿都先精簡/壓縮過。
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest


def _import_module():
    mod_name = "scripts.analyze_daily_log"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    base = Path(__file__).parent.parent
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    return importlib.import_module(mod_name)


# ── 1. compress_stt_lines ────────────────────────────────────────────────────

def test_compress_stt_lines():
    mod = _import_module()
    lines = [
        "2026-10-21 12:13:59,189 - [showay] (Debounced) 好你知道 Brotoo這個遊戲嗎",
        "2026-10-21 12:14:00,000 - [狗與露] (Debounced) 嗯",
        "2026-10-21 12:14:01,000 - [狗與露] (Debounced) 對啊！",
        "2026-10-21 12:15:00,000 - [狗與露] (Debounced) 我要辛拉麵加雞排",
        "2026-10-21 12:15:00,000 - [狗與露] (Debounced) 我要辛拉麵加雞排",
        "2026-10-20 22:02:59,712 - [點歌-語音] 使用者=showay | 搜尋=夜空中最亮的星 | 結果=逃跑計劃 - 夜空中最亮的星",
        "2026-10-20 15:12:02,965 - [BOT回台→showay] showay，你回來啦",
        "2026-10-22 12:14:26,290 - [⚡喚醒] [showay] raw='馬文你知道幫註那種戲嗎' | Track=A",
        "=== STT LOG (2026-10-20 12:00 ~ 2026-10-21 12:00) ===",
    ]
    out = mod.compress_stt_lines(lines)
    assert out == [
        "12:13 showay: 好你知道 Brotoo這個遊戲嗎",
        "12:15 狗與露: 我要辛拉麵加雞排",
        "22:02 showay 點歌：逃跑計劃 - 夜空中最亮的星",
    ]


# ── 2. compact_players_for_prompt ───────────────────────────────────────────

def test_compact_players_for_prompt():
    mod = _import_module()
    players = {
        "狗與露": {
            "taste": {"草東": {"score": 1.0}, "草東沒有派對": {"score": 4.0}},
            "speech_dna": {"style_summary": "x"},
            "news_queue": [{"text": "y"}],
            "stats": {"interaction_count": 5},
            "likes": ["草東沒有派對"],
            "personal_info": {"food": "拉麵", "age": None, "hobbies": []},
            "suki_impression": "舊印象",
            "relationship_stage": None,
        },
        "馬文": "not-a-dict",
    }
    out = mod.compact_players_for_prompt(players)
    assert set(out.keys()) == {"狗與露"}   # 非 dict 玩家跳過
    entry = out["狗與露"]
    assert set(entry.keys()) == {"likes", "mentioned", "personal_info", "suki_impression"}
    assert entry["likes"] == ["草東沒有派對"]
    # 只送 taste 項目名稱（不送分數）、扣掉已投影成 likes/dislikes 的——讓模型沿用既有寫法累積分數
    assert entry["mentioned"] == ["草東"]
    assert entry["personal_info"] == {"food": "拉麵"}
    assert entry["suki_impression"] == "舊印象"


def test_prompt_keeps_taste_accumulation_rule():
    # 跨日重複提到同一喜好要沿用既有字串，taste 分數才會累積升級（見 test_taste_decay F3）
    mod = _import_module()
    assert "含以前提過的" in mod.SYSTEM_PROMPT
    assert "mentioned" in mod.SYSTEM_PROMPT


# ── 3. delta_to_update ───────────────────────────────────────────────────────

def test_delta_to_update_likes_dislikes_taboos():
    mod = _import_module()
    delta = {
        "new_likes": ["草東沒有派對", "", "  ", "周杰倫 "],
        "new_dislikes": [],
        "new_taboos": ["離婚"],
    }
    out = mod.delta_to_update(delta, now=123.0)
    assert out["likes"] == ["草東沒有派對", "周杰倫"]
    assert "dislikes" not in out
    assert out["taboos"] == ["離婚"]


def test_delta_to_update_caps_runaway_lists():
    # 10/23 真模型實測：放寬「含以前提過的」後 showay 的 new_dislikes 一次吐 70 項「遊戲中X」垃圾、
    # hobbies 灌一大串——prompt 管不住就由程式碼硬上限擋，免得汙染 taste 分數
    mod = _import_module()
    delta = {
        "new_dislikes": [f"遊戲中蟲{i}" for i in range(70)],
        "personal_info": {"hobbies": [f"嗜好{i}" for i in range(12)], "food": "拉麵"},
    }
    out = mod.delta_to_update(delta, now=1.0)
    assert out["dislikes"] == [f"遊戲中蟲{i}" for i in range(mod.TASTE_MAX_PER_DAY)]
    assert out["personal_info"]["hobbies"] == [f"嗜好{i}" for i in range(mod.PERSONAL_LIST_MAX)]
    assert out["personal_info"]["food"] == "拉麵"


def test_prompt_requires_explicit_preference():
    mod = _import_module()
    assert "明確說出" in mod.SYSTEM_PROMPT
    assert "最多 5 項" in mod.SYSTEM_PROMPT


def test_delta_to_update_truncates_impression_and_highlight():
    mod = _import_module()
    long_impression = "x" * (mod.IMPRESSION_MAX_CHARS + 50)
    long_highlight = "y" * (mod.HIGHLIGHT_MAX_CHARS + 50)
    delta = {"suki_impression": long_impression, "highlight_of_the_day": long_highlight}
    out = mod.delta_to_update(delta, now=123.0)
    assert out["suki_impression"] == long_impression[: mod.IMPRESSION_MAX_CHARS]
    assert len(out["suki_impression"]) == mod.IMPRESSION_MAX_CHARS
    assert out["highlight_of_the_day"] == long_highlight[: mod.HIGHLIGHT_MAX_CHARS]
    assert len(out["highlight_of_the_day"]) == mod.HIGHLIGHT_MAX_CHARS


def test_delta_to_update_emotional_highlights():
    mod = _import_module()
    delta = {
        "emotional_highlights": [
            {"moment": "", "valence": "warm"},
            {"moment": "開心聊到辛拉麵", "valence": "funny"},
            "not-a-dict",
            {"moment": "聊到工作壓力"},
            {"moment": "第三個合法的情緒時刻（超過上限要被丟掉）", "valence": "sad"},
        ],
    }
    out = mod.delta_to_update(delta, now=999.0)
    assert len(out["emotional_highlights"]) == mod.EMOTION_MAX_PER_DAY
    first, second = out["emotional_highlights"]
    assert first == {"moment": "開心聊到辛拉麵", "valence": "funny", "timestamp": 999.0}
    assert second == {"moment": "聊到工作壓力", "valence": "warm", "timestamp": 999.0}


def test_delta_to_update_drops_unknown_keys():
    mod = _import_module()
    delta = {"speech_dna": {"x": 1}, "stats": {"interaction_count": 5}, "new_likes": ["x"]}
    out = mod.delta_to_update(delta, now=1.0)
    assert "speech_dna" not in out
    assert "stats" not in out
    assert out == {"likes": ["x"]}


def test_delta_to_update_non_dict_returns_empty():
    mod = _import_module()
    assert mod.delta_to_update(None, now=1.0) == {}
    assert mod.delta_to_update("x", now=1.0) == {}
    assert mod.delta_to_update([], now=1.0) == {}


# ── 4. delta_to_update + merge_player ───────────────────────────────────────

def test_delta_to_update_merges_into_existing_player():
    mod = _import_module()
    existing = {
        "likes": ["周杰倫"],
        "suki_impression": "舊的很長的印象",
        "highlight_of_the_day": "舊亮點",
        "speech_dna": {"x": 1},
    }
    delta = {
        "new_likes": ["草東沒有派對"],
        "suki_impression": "新印象",
        "highlight_of_the_day": "今天的事",
    }
    merged = mod.merge_player(existing, mod.delta_to_update(delta, now=1.0))
    assert "草東沒有派對" in merged["taste"]
    assert merged["suki_impression"] == "新印象"
    assert merged["highlight_of_the_day"] == "今天的事"
    assert merged["speech_dna"] == {"x": 1}


# ── 5. main() 端到端 ─────────────────────────────────────────────────────────

@pytest.fixture
def delta_review_env(tmp_path, monkeypatch):
    mod = _import_module()
    log_dir = tmp_path / "records" / "daily"
    log_dir.mkdir(parents=True)
    (log_dir / "2026-09-17.log").write_text(
        "=== STT LOG (2026-09-16 12:00 ~ 2026-09-17 12:00) ===\n"
        + "".join(
            f"2026-09-17 20:0{i}:00,000 - [狗與露] (Debounced) 我最近很愛聽草東\n"
            for i in range(6)
        ),
        encoding="utf-8",
    )
    feedback = tmp_path / "records" / "response_feedback.jsonl"
    feedback.write_text("", encoding="utf-8")
    memory_file = tmp_path / "suki_memory.json"
    memory_file.write_text(json.dumps({
        "players": {"狗與露": {"likes": ["周杰倫"], "suki_impression": "舊印象"}},
        "_meta": {"review_date": "2026-09-16"},
    }, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(mod, "BASE_DIR", tmp_path)
    monkeypatch.setattr(mod, "LOG_DIR", log_dir)
    monkeypatch.setattr(mod, "FEEDBACK_FILE", feedback)
    monkeypatch.setattr(mod, "MEMORY_FILE", memory_file)
    monkeypatch.setattr(mod, "BACKUP_DIR", tmp_path / "records" / "backups")
    monkeypatch.setattr(mod, "GOOGLE_API_KEY", "x")
    monkeypatch.setattr(mod, "persist_players_to_db", lambda *a, **k: 0)
    monkeypatch.setattr(mod, "decay_players_in_db", lambda *a, **k: [])
    monkeypatch.setattr(mod, "build_stt_corrections_dict", lambda *a, **k: {})
    monkeypatch.setattr(mod, "notify_discord_review", lambda **k: None)
    monkeypatch.setattr(sys, "argv", ["analyze_daily_log.py", "--date", "2026-09-17"])

    sent = {}
    # 正式模型（REVIEW_SCHEMA）輸出的是玩家陣列
    fake_result = {
        "players": [
            {"name": "狗與露", "new_likes": ["草東沒有派對"]},
            {"name": "馬文", "new_likes": ["x"]},
            {"name": "路人甲", "new_likes": ["y"]},
        ],
    }

    def fake_call(user_content, paid_call=None):
        sent["user_content"] = user_content
        return fake_result

    monkeypatch.setattr(mod, "call_review_llm", fake_call)
    mod.main()
    return mod, sent, json.loads(memory_file.read_text(encoding="utf-8")), tmp_path


def test_main_sends_compressed_stt_and_compact_memory(delta_review_env):
    _, sent, _, _ = delta_review_env
    content = sent["user_content"]
    assert "20:00 狗與露: 我最近很愛聽草東" in content
    assert "(Debounced)" not in content
    assert "taste" not in content


def test_main_only_updates_active_or_existing_players(delta_review_env):
    _, _, final, _ = delta_review_env
    player = final["players"]["狗與露"]
    assert "草東沒有派對" in player["taste"]
    assert "馬文" not in final["players"]
    assert "路人甲" not in final["players"]


# ── 6. call_review_llm max_tokens ───────────────────────────────────────────

def _fake_paid_call(returns):
    seq = returns if isinstance(returns, list) else [returns]
    calls = {"n": 0, "kwargs": []}

    async def _paid_call(content, **kwargs):
        calls["kwargs"].append(kwargs)
        r = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        return r

    _paid_call.calls = calls
    return _paid_call


def test_call_review_llm_uses_max_tokens_6000():
    mod = _import_module()
    paid = _fake_paid_call(json.dumps({"players": {}}))
    mod.call_review_llm("x", paid_call=paid)
    assert paid.calls["kwargs"][0]["max_tokens"] == 6000


# ── 7. 10/23 真模型實測後的修正：3.1-flash-lite＋response_schema＋玩家陣列＋句尾截斷 ──

def test_review_uses_lite_model_and_schema():
    # 2.5-flash 關思考吃整天逐字稿會退化（列 70 項／tab 迴圈撞上限）；3.1-flash-lite＋schema 正常且最便宜
    mod = _import_module()
    paid = _fake_paid_call(json.dumps({"players": []}))
    mod.call_review_llm("x", paid_call=paid)
    kw = paid.calls["kwargs"][0]
    assert mod.REVIEW_MODEL == "gemini-3.1-flash-lite"
    assert kw["model"] == mod.REVIEW_MODEL
    assert kw["response_schema"] is mod.REVIEW_SCHEMA


def test_review_schema_caps_lists():
    mod = _import_module()
    item = mod.REVIEW_SCHEMA["properties"]["players"]["items"]["properties"]
    for k in ("new_likes", "new_dislikes", "new_taboos"):
        assert item[k]["maxItems"] == mod.TASTE_MAX_PER_DAY
    assert item["emotional_highlights"]["maxItems"] == mod.EMOTION_MAX_PER_DAY
    assert "name" in mod.REVIEW_SCHEMA["properties"]["players"]["items"]["required"]


def test_players_from_result_list_and_dict():
    mod = _import_module()
    lst = {"players": [{"name": "狗與露", "new_likes": ["a"]}, {"new_likes": ["沒名字"]}, "垃圾"]}
    assert mod.players_from_result(lst) == {"狗與露": {"new_likes": ["a"]}}
    assert mod.players_from_result({"players": {"showay": {"new_likes": ["b"]}}}) == {"showay": {"new_likes": ["b"]}}
    assert mod.players_from_result({}) == {}
    assert mod.players_from_result({"players": "x"}) == {}


def test_impression_clipped_at_sentence_end():
    mod = _import_module()
    n = mod.IMPRESSION_MAX_CHARS
    first = "甲" * (n - 20) + "。"
    text = first + "乙" * 50
    out = mod.delta_to_update({"suki_impression": text}, now=1.0)
    assert out["suki_impression"] == first   # 退到最後一個句號，不斷在句子中間


def test_compact_memory_truncates_old_impression():
    # 舊印象累積到上千字，整段送進去模型就照抄再接——只送前 IMPRESSION_MAX_CHARS 字
    mod = _import_module()
    out = mod.compact_players_for_prompt({"狗與露": {"suki_impression": "舊" * 1000}})
    assert out["狗與露"]["suki_impression"] == "舊" * mod.IMPRESSION_MAX_CHARS

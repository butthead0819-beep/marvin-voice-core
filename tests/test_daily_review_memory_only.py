"""Daily review 瘦身：只保留玩家記憶萃取（2026-09-18 拍板）。

背景：輸出每次撞 16000 max_tokens 截斷 → 修復失敗 → 重試，一天打 2-6 次付費呼叫；
輸出裡 proactive_topics / marvin_performance(LLM 質性分析) / wake_analysis /
system_suggestions / atmosphere_calibration 不是記憶萃取，砍掉。

規則：
  1. SYSTEM_PROMPT 只要求 players + _meta，不再要求被砍的區段
  2. 送出的 user content 不含話題/主動發言統計段落，也不含頂層 meta（只送 players）
  3. LLM 就算還吐被砍的區段，也不寫進 suki_memory.json
  4. 被砍且有 runtime 消費者的舊 key（proactive_topics / wake_analysis /
     system_suggestions）從 suki_memory.json 移除——否則 review_date 照常推進，
     ProactiveTopicAgent 的過期判斷永遠不會擋，會一直重演凍結的舊節目
  5. atmosphere_calibration 既有關鍵字保留、不再更新；wake_words_override 不再被寫
  6. 玩家記憶照常合併、_meta.review_date 照常推進
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

REMOVED_SECTIONS = ("proactive_topics", "marvin_performance", "wake_analysis",
                    "system_suggestions", "atmosphere_calibration")


def _import_module():
    mod_name = "scripts.analyze_daily_log"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    base = Path(__file__).parent.parent
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    return importlib.import_module(mod_name)


def test_system_prompt_only_asks_for_memory():
    mod = _import_module()
    for key in REMOVED_SECTIONS + ("prompt_suggestions", "problem_patterns", "wake_words"):
        assert key not in mod.SYSTEM_PROMPT, f"SYSTEM_PROMPT 仍要求 {key}"
    for key in ('"players"', "likes", "taboos", "suki_impression", "emotional_highlights",
                "speech_dna", "news_queue", "_meta"):
        assert key in mod.SYSTEM_PROMPT, f"SYSTEM_PROMPT 漏了記憶欄位 {key}"


@pytest.fixture
def review_env(tmp_path, monkeypatch):
    mod = _import_module()
    log_dir = tmp_path / "records" / "daily"
    log_dir.mkdir(parents=True)
    (log_dir / "2026-09-17.log").write_text(
        "=== STT LOG (2026-09-16 12:00 ~ 2026-09-17 12:00) ===\n"
        + "".join(f"[20:0{i}:00] 狗與露 | raw: 我最近很愛聽草東 | clean: 我最近很愛聽草東\n"
                  for i in range(6)),
        encoding="utf-8",
    )
    feedback = tmp_path / "records" / "response_feedback.jsonl"
    feedback.write_text("", encoding="utf-8")
    memory_file = tmp_path / "suki_memory.json"
    memory_file.write_text(json.dumps({
        "players": {"狗與露": {"likes": ["周杰倫"], "suki_impression": "舊印象"}},
        "proactive_topics": [{"id": "marvin_manzai", "title": "雙口漫才表演"}],
        "wake_analysis": {"suggested_additions": []},
        "system_suggestions": [{"content": "舊建議"}],
        "atmosphere_calibration": {"suggested_additions": {"music": ["舊關鍵字"]}},
        "marvin_performance": {"score": 5.0, "optimal_response_length": 40},
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
    # 模擬 LLM 不聽話、還是吐出被砍的區段
    fake_result = {
        "players": {"狗與露": {"likes": ["周杰倫", "草東沒有派對"], "suki_impression": "新印象"}},
        "proactive_topics": [{"id": "marvin_sing", "title": "即興自彈自唱"}],
        "marvin_performance": {"score": 9.9, "problem_patterns": [{"pattern": "x"}]},
        "wake_analysis": {"suggested_additions": ["馬溫"], "suggested_removals": []},
        "system_suggestions": [{"content": "新建議"}],
        "atmosphere_calibration": {"suggested_additions": {"music": ["新關鍵字"]}},
        "_meta": {"review_date": "2026-09-17"},
    }

    def fake_call(user_content, paid_call=None):
        sent["user_content"] = user_content
        return fake_result

    monkeypatch.setattr(mod, "call_review_llm", fake_call)
    mod.main()
    return mod, sent, json.loads(memory_file.read_text(encoding="utf-8")), tmp_path


def test_user_content_has_no_meta_or_stats_sections(review_env):
    _, sent, _, _ = review_env
    content = sent["user_content"]
    for key in REMOVED_SECTIONS + ("主動發言效益", "話題關鍵字標記統計"):
        assert key not in content, f"user content 仍送出 {key}"
    assert "狗與露" in content and "舊印象" in content  # 玩家記憶仍要送


def test_removed_sections_not_written(review_env):
    _, _, final, _ = review_env
    for key in ("proactive_topics", "wake_analysis", "system_suggestions"):
        assert key not in final, f"{key} 應從 suki_memory.json 移除"
    assert final["atmosphere_calibration"]["suggested_additions"] == {"music": ["舊關鍵字"]}
    assert final["marvin_performance"].get("score") != 9.9
    assert final["marvin_performance"].get("optimal_response_length") == 40


def test_wake_override_not_written(review_env):
    _, _, _, tmp_path = review_env
    assert not (tmp_path / "records" / "wake_words_override.json").exists()


def test_player_memory_still_merged(review_env):
    _, _, final, _ = review_env
    player = final["players"]["狗與露"]
    assert "草東沒有派對" in player["taste"]  # 新喜好先進 taste（跨日累積才投影成 likes）
    assert player["suki_impression"] == "新印象"
    assert final["_meta"]["review_date"] == "2026-09-17"

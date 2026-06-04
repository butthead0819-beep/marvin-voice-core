"""Shared test isolation.

Redirect stt_cleaner 的所有寫檔路徑到 tmp，讓任何測試都不會污染 prod records/
（feedback_stt_test_isolation：cleaner 測試曾寫到真 records/）。autouse → 每個測試生效。
"""
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_stt_cleaner_writes(tmp_path, monkeypatch):
    try:
        import stt_cleaner  # noqa: F401
    except Exception:
        return
    for attr, fn in (("_CORRECTIONS_LOG", "corr.jsonl"),
                     ("_LOCAL_CORRECTIONS_PATH", "corr.json"),
                     ("_GATE_DROP_LOG", "gate_drops.jsonl")):
        if hasattr(stt_cleaner, attr):
            monkeypatch.setattr(f"stt_cleaner.{attr}", tmp_path / fn, raising=False)
    yield


@pytest.fixture(autouse=True)
def _isolate_record_writes(tmp_path, monkeypatch):
    """防測試污染 prod records/。

    2026-05-30：發現 judge_outcomes / agent_gaps 被測試寫滿 fixture（`test query`、
    `今天天氣怎麼樣` 等），污染 daily ritual 指標（agent_gaps 78% 是測試垃圾）。
    根因：run_shadow_race / GapLogger 寫死 records/ 路徑，多個測試建真 VoiceController
    跑 wake path 就寫進真檔。

    這個 autouse fixture 把任何「relative 且開頭 records/」的寫入導到 tmp，
    任何測試都不可能再污染。測試自己用的 tmp_path（絕對路徑）不受影響。
    """
    records_dir = tmp_path / "records"
    records_dir.mkdir(exist_ok=True)

    def _redirect(path):
        p = Path(path)
        # 只導「relative 且第一段是 records」的 prod 路徑；測試自己的 tmp 絕對路徑不動
        if not p.is_absolute() and p.parts and p.parts[0] == "records":
            return records_dir / Path(*p.parts[1:])
        return p

    # 1. GapLogger (agent_gaps) — __init__ runtime 建，patch class method 通用
    try:
        import intent_gap
        _gap_init = intent_gap.GapLogger.__init__

        def _gap_safe(self, jsonl_path, *a, **kw):
            _gap_init(self, _redirect(jsonl_path), *a, **kw)
        monkeypatch.setattr(intent_gap.GapLogger, "__init__", _gap_safe)
    except Exception:
        pass

    # 2. RescueOutcomeLogger (rescue_outcomes) — 防禦性
    try:
        import intent_agents.rescue_outcome_logger as rol
        _rol_init = rol.RescueOutcomeLogger.__init__

        def _rol_safe(self, jsonl_path):
            _rol_init(self, _redirect(jsonl_path))
        monkeypatch.setattr(rol.RescueOutcomeLogger, "__init__", _rol_safe)
    except Exception:
        pass

    # 3. judge_outcomes — run_shadow_race 的 outcome_path default 在 import 綁定，
    #    patch module const 無效；改 wrap 真正寫檔的 write_race_outcome（vi namespace）
    try:
        import intent_judges.voice_integration as vi
        _wro = vi.write_race_outcome

        def _wro_safe(path, *a, **kw):
            return _wro(_redirect(path), *a, **kw)
        monkeypatch.setattr(vi, "write_race_outcome", _wro_safe)
    except Exception:
        pass

    # 4. speak_outcomes — append_speak_outcome 的 path arg 重導
    try:
        import speak_outcome
        _aso = speak_outcome.append_speak_outcome

        def _aso_safe(rec, path=speak_outcome.DEFAULT_LOG_PATH, *a, **kw):
            return _aso(rec, _redirect(path), *a, **kw)
        monkeypatch.setattr(speak_outcome, "append_speak_outcome", _aso_safe)
    except Exception:
        pass

    # 5. llm_routing (llm_agents.metrics._LOG_PATH) — log_dispatch 寫死模組常數，跑 bus
    #    dispatch 的測試會用 test-name purpose 寫進 prod records/llm_routing.jsonl
    #    （2026-06-04 發現：24h 287 筆有 129 筆測試污染，把回應 LLM 成功率灌爆）。
    try:
        import llm_agents.metrics as _llm_metrics
        monkeypatch.setattr(_llm_metrics, "_LOG_PATH",
                            records_dir / "llm_routing.jsonl", raising=False)
    except Exception:
        pass

    yield

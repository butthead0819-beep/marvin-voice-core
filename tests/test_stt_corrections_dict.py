"""build_stt_corrections_dict 的結構正確性（2026-06-04 A：修遞迴巢狀腐爛）。

bug：原寫檔 `existing = json.loads(file)`（整包含 _updated/corrections）→
`existing.update(corrections)`（flat pairs 加到頂層）→
`write({"corrections": existing})`（又把整包含舊 corrections 包進新 corrections）。
每跑一次多巢一層，stt_corrections.json 無限長大、reader 只看頂層。

正解：讀進來只取內層 corrections dict、flat merge、寫回 flat string→string。
順手 sanitize：丟掉非 str→str、丟掉 _updated/corrections 殘留 key。
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path


def _import_module():
    mod_name = "scripts.analyze_daily_log"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    base = Path(__file__).parent.parent
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    return importlib.import_module(mod_name)


def _seed_jsonl(path: Path, pairs: list[tuple[str, str]]):
    path.write_text(
        "\n".join(json.dumps({"raw": r, "clean": c}, ensure_ascii=False) for r, c in pairs),
        encoding="utf-8",
    )


def _read_corr(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ── 1. 跑一次：結構是 flat {raw:clean}，corrections 內無 _updated/corrections 殘留 ──
def test_build_writes_flat_corrections(tmp_path, monkeypatch):
    mod = _import_module()
    jsonl = tmp_path / "stt_corrections.jsonl"
    jsonp = tmp_path / "stt_corrections.json"
    # 同一對出現 2 次 → 過 _MIN_FREQ=2
    _seed_jsonl(jsonl, [("嘿Siri", "嘿馬文"), ("嘿Siri", "嘿馬文")])
    monkeypatch.setattr(mod, "_CORRECTIONS_JSONL", jsonl)
    monkeypatch.setattr(mod, "_CORRECTIONS_JSON", jsonp)

    mod.build_stt_corrections_dict()
    doc = _read_corr(jsonp)
    corr = doc["corrections"]
    assert corr == {"嘿Siri": "嘿馬文"}
    assert "corrections" not in corr      # 沒把自己巢進去
    assert "_updated" not in corr         # meta 沒漏進 pairs


# ── 2. 跑兩次：不會越巢越深（冪等結構） ──
def test_build_twice_does_not_nest(tmp_path, monkeypatch):
    mod = _import_module()
    jsonl = tmp_path / "stt_corrections.jsonl"
    jsonp = tmp_path / "stt_corrections.json"
    _seed_jsonl(jsonl, [("嘿Siri", "嘿馬文"), ("嘿Siri", "嘿馬文")])
    monkeypatch.setattr(mod, "_CORRECTIONS_JSONL", jsonl)
    monkeypatch.setattr(mod, "_CORRECTIONS_JSON", jsonp)

    mod.build_stt_corrections_dict()
    # 第二次再加一對
    _seed_jsonl(jsonl, [("嘿Siri", "嘿馬文"), ("嘿Siri", "嘿馬文"),
                        ("Marvin", "馬文"), ("Marvin", "馬文")])
    mod.build_stt_corrections_dict()

    corr = _read_corr(jsonp)["corrections"]
    assert corr == {"嘿Siri": "嘿馬文", "Marvin": "馬文"}
    # 所有 value 都是字串（沒有任何 nested dict）
    assert all(isinstance(v, str) for v in corr.values())


# ── 3. 餵已腐爛的巢狀舊檔 → 復原成 flat（撈回內層真 pairs，丟巢狀垃圾） ──
def test_build_recovers_corrupted_nested_file(tmp_path, monkeypatch):
    mod = _import_module()
    jsonl = tmp_path / "stt_corrections.jsonl"
    jsonp = tmp_path / "stt_corrections.json"
    _seed_jsonl(jsonl, [("Marvin", "馬文"), ("Marvin", "馬文")])
    # 模擬 prod 那種遞迴巢狀腐爛檔
    corrupted = {
        "_updated": "2026-06-04",
        "corrections": {
            "馬文播放": "馬文，播放",          # 真 pair
            "_updated": "2026-06-02",          # 垃圾
            "corrections": {                    # 巢狀垃圾
                "Okay.": "喔",
                "corrections": {"嘿Siri": "嘿馬文"},
            },
        },
    }
    jsonp.write_text(json.dumps(corrupted, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(mod, "_CORRECTIONS_JSONL", jsonl)
    monkeypatch.setattr(mod, "_CORRECTIONS_JSON", jsonp)

    mod.build_stt_corrections_dict()
    corr = _read_corr(jsonp)["corrections"]
    # 撈回各層真 pair + 今日新 pair，且全 flat、無 _updated/corrections key
    assert corr["馬文播放"] == "馬文，播放"
    assert corr["Okay."] == "喔"
    assert corr["嘿Siri"] == "嘿馬文"
    assert corr["Marvin"] == "馬文"
    assert "corrections" not in corr
    assert "_updated" not in corr
    assert all(isinstance(v, str) for v in corr.values())


# ── 4. 歧義 raw（同 raw 多個分歧 clean，無主導）→ 整條丟掉，不寫進字典 ──
#     防 exact-match 快取把「馬文播放音樂」改寫成「播放周杰倫」點錯歌。
def test_ambiguous_raw_is_dropped(tmp_path, monkeypatch):
    mod = _import_module()
    jsonl = tmp_path / "stt_corrections.jsonl"
    jsonp = tmp_path / "stt_corrections.json"
    pairs = []
    # 歧義：同 raw 三種 clean 各 3 次（無單一主導）→ 應丟
    for _ in range(3):
        pairs += [("馬文播放音樂", "馬文，播放音樂"),
                  ("馬文播放音樂", "馬文，播放周杰倫"),
                  ("馬文播放音樂", "馬文，第一次 70b")]
    # 穩定：同 raw 單一主導 → 應留
    for _ in range(5):
        pairs.append(("Okay.", "喔"))
    _seed_jsonl(jsonl, pairs)
    monkeypatch.setattr(mod, "_CORRECTIONS_JSONL", jsonl)
    monkeypatch.setattr(mod, "_CORRECTIONS_JSON", jsonp)

    mod.build_stt_corrections_dict()
    corr = _read_corr(jsonp)["corrections"]
    assert "馬文播放音樂" not in corr   # 歧義丟掉
    assert corr["Okay."] == "喔"        # 穩定保留

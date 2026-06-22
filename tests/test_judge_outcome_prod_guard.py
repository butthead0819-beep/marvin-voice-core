"""write_race_outcome 不得在 pytest 下污染 prod records/judge_outcomes.jsonl。

2026-06-22：judge_outcomes.jsonl 累積 972/1364 筆（82%）是 Alice probe 合成資料，
讓每日效果分析失真。根因：某些 pytest run 下 tests/conftest.py 的 relative→tmp
path 重導未生效，relative default 路徑直接落進真 records/。

修法是 writer 層 defense-in-depth：pytest 環境（PYTEST_CURRENT_TEST process-wide）
下拒絕寫入 *relative* 路徑。合法測試一律用 absolute tmp_path，不受影響。
"""
from types import SimpleNamespace

from intent_judges.telemetry import write_race_outcome


def _fake_ctx():
    return SimpleNamespace(speaker="Alice", mode="normal", query="播放陶喆的天天")


def _fake_result():
    bid = SimpleNamespace(name="music", confidence=0.8, reason="probe")
    outcome = SimpleNamespace(
        name="j1_regex", status="completed", latency_ms=0.9, bid=bid, error=None
    )
    return SimpleNamespace(
        winning_judge="j1_regex",
        winner=bid,
        total_ms=1.0,
        outcomes=[outcome],
    )


def test_relative_path_is_dropped_under_pytest(tmp_path, monkeypatch):
    """relative records/ 路徑（逃過 conftest 重導）在 pytest 下 no-op，不建檔。"""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "dummy::test")
    # chdir 到 tmp，萬一 guard 失效也只污染 tmp、不碰真 prod
    monkeypatch.chdir(tmp_path)
    rel = type(tmp_path)("records/judge_outcomes.jsonl")

    write_race_outcome(rel, "u1", _fake_ctx(), _fake_result())

    assert not (tmp_path / "records" / "judge_outcomes.jsonl").exists()


def test_absolute_tmp_path_still_writes_under_pytest(tmp_path, monkeypatch):
    """合法測試用 absolute tmp_path —— 照常寫入，guard 不擋。"""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "dummy::test")
    out = tmp_path / "outcomes.jsonl"

    write_race_outcome(out, "u2", _fake_ctx(), _fake_result())

    assert out.exists()
    assert len(out.read_text(encoding="utf-8").strip().splitlines()) == 1

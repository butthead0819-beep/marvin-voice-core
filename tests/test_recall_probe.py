"""TDD: recall probe（Phase 4）— 對已知 ground truth 查 Marvin 記憶、比對、算正確率。

純函式 evaluate_case / run_probe，memory 注入 fake（不碰真實 suki_memory，不污染）。
"""
from __future__ import annotations

from scripts.recall_probe import evaluate_case, run_probe


class _FakeMemory:
    """mirror MemoryManager.has_player + get_player_memory（無建立副作用）。"""

    def __init__(self, players: dict):
        self._players = players

    def has_player(self, name: str) -> bool:
        return name in self._players

    def get_player_memory(self, name: str) -> dict:
        return self._players[name]


def _mem():
    return _FakeMemory({"大肚": {"likes": ["周杰倫 夜曲", "孫燕姿"], "dislikes": ["重金屬"]}})


# ── evaluate_case ──────────────────────────────────────────────────────────

def test_case_hit_when_keyword_in_field():
    c = {"speaker": "大肚", "field": "likes", "expect_any": ["周杰倫"]}
    assert evaluate_case(c, _mem()) is True


def test_case_miss_when_keyword_absent():
    c = {"speaker": "大肚", "field": "likes", "expect_any": ["林俊傑"]}
    assert evaluate_case(c, _mem()) is False


def test_case_checks_correct_field():
    c = {"speaker": "大肚", "field": "dislikes", "expect_any": ["重金屬"]}
    assert evaluate_case(c, _mem()) is True


def test_unknown_speaker_is_miss_no_side_effect():
    """不存在的 speaker → miss，且不得呼叫 get_player_memory（避免建立副作用）。"""
    called = {"got": False}

    class _M(_FakeMemory):
        def get_player_memory(self, name):
            called["got"] = True
            return super().get_player_memory(name)

    c = {"speaker": "幽靈", "field": "likes", "expect_any": ["x"]}
    assert evaluate_case(c, _M({"大肚": {"likes": []}})) is False
    assert called["got"] is False   # has_player gate 擋住，沒觸發 get


def test_any_keyword_hits():
    c = {"speaker": "大肚", "field": "likes", "expect_any": ["不存在", "孫燕姿"]}
    assert evaluate_case(c, _mem()) is True


# ── run_probe ──────────────────────────────────────────────────────────────

def test_run_probe_accuracy_no_record():
    cases = [
        {"speaker": "大肚", "field": "likes", "expect_any": ["周杰倫"]},   # hit
        {"speaker": "大肚", "field": "likes", "expect_any": ["林俊傑"]},   # miss
    ]
    summ = run_probe(cases, _mem(), record=False)
    assert summ["total"] == 2
    assert summ["correct"] == 1
    assert summ["accuracy"] == 0.5
    assert len(summ["results"]) == 2

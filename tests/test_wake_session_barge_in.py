"""WakeSession 的 barge-in 狀態機（note_barge_in / mark_last_answer_interrupted /
on_utterance 的 barge 分支）。純核心，不走 IO，`now` 手動推進。"""
from __future__ import annotations

from barge_in import BARGE_PENDING_TTL_S
from wake_session import MAX_QA_TURNS, WakeSession


def _opened_session(speaker="A", t0=0.0):
    s = WakeSession()
    s.on_wake_accepted(speaker, t0)
    s.on_replied(t0)
    return s


def test_barge_pending_half_sentence_not_final_not_captured():
    s = _opened_session()
    s.note_barge_in(1.0)
    assert s.on_utterance("A", 1.1, marvin_speaking=True, final=False) is False
    assert s.barge_in_pending_at == 1.0   # pending 還在
    assert s.barge_captured is False


def test_barge_pending_final_sentence_captured_and_cleared():
    s = _opened_session()
    s.note_barge_in(1.0)
    assert s.on_utterance("A", 1.1, marvin_speaking=True, final=False) is False
    assert s.on_utterance("A", 1.5, marvin_speaking=True, final=True) is True
    assert s.barge_captured is True
    assert s.barge_in_pending_at is None


def test_non_target_speech_does_not_touch_pending():
    s = _opened_session()
    s.note_barge_in(1.0)
    assert s.on_utterance("B", 1.1, marvin_speaking=True, final=True) is False
    assert s.barge_in_pending_at == 1.0


def test_pending_expires_after_ttl():
    s = _opened_session()
    s.note_barge_in(1.0)
    now = 1.0 + BARGE_PENDING_TTL_S + 0.1
    assert s.on_utterance("A", now, marvin_speaking=True, final=True) is False
    assert s.barge_in_pending_at is None


def test_barge_not_blocked_by_awaiting_since():
    s = _opened_session()
    s.awaiting_since = 5.0   # 剛接住一句還沒回應
    before = s.unanswered
    s.note_barge_in(6.0)
    assert s.on_utterance("A", 6.1, marvin_speaking=True, final=True) is True
    assert s.unanswered == before   # 不算 unanswered


def test_barge_pending_closing_phrase_ends_session():
    s = _opened_session()
    s.note_barge_in(1.0)
    assert s.on_utterance("A", 1.1, marvin_speaking=True, text="好了", final=True) is False
    assert s.target is None


def test_note_barge_in_noop_when_not_opened_or_no_target():
    s = WakeSession()
    s.note_barge_in(1.0)
    assert s.barge_in_pending_at is None

    s2 = WakeSession()
    s2.on_wake_accepted("A", 0.0)   # armed 但未 opened
    s2.note_barge_in(1.0)
    assert s2.barge_in_pending_at is None


# ── mark_last_answer_interrupted ───────────────────────────────────────────────

def test_mark_last_answer_interrupted_appends_to_existing_qa_turn():
    s = _opened_session()
    s.qa_turns = [("問題", "答案")]
    s.mark_last_answer_interrupted("未說完")
    assert s.qa_turns[-1] == ("問題", "答案（念到一半被使用者打斷）")


def test_mark_last_answer_interrupted_appends_new_turn_when_empty():
    s = _opened_session()
    s.mark_last_answer_interrupted("未說完的話")
    assert s.qa_turns == [("", "未說完的話（念到一半被使用者打斷）")]


def test_mark_last_answer_interrupted_append_path_stays_within_max_qa_turns():
    # append 分支只在 qa_turns 空時觸發（一次最多 append 一輪），結果必然 <= MAX_QA_TURNS。
    s = _opened_session()
    s.mark_last_answer_interrupted("x")
    assert len(s.qa_turns) == 1 <= MAX_QA_TURNS
    # 之後 qa_turns 非空，重複呼叫只改最後一輪，不會無限增長
    for _ in range(MAX_QA_TURNS + 2):
        s.mark_last_answer_interrupted("x")
    assert len(s.qa_turns) == 1


def test_mark_last_answer_interrupted_noop_without_target():
    s = WakeSession()
    s.mark_last_answer_interrupted("x")
    assert s.qa_turns == []


# ── _end 清除 barge 狀態 ────────────────────────────────────────────────────────

def test_end_clears_barge_state():
    s = _opened_session()
    s.note_barge_in(1.0)
    s.barge_captured = True
    s._end("test")
    assert s.barge_in_pending_at is None
    assert s.barge_captured is False

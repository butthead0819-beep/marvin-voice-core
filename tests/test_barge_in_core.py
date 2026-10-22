"""barge_in.py 純核心單測：confirm_step / user_done / interrupted_text / clean_confirm。"""
from __future__ import annotations

from barge_in import (
    BARGE_CONFIRM_S, CONFIRM_MAX_CHARS, DIP_GAP_S, INTERRUPTED_TEXT_MAX_CHARS,
    USER_DONE_GAP_S, clean_confirm, confirm_step, interrupted_text, user_done,
)


# ── confirm_step ──────────────────────────────────────────────────────────────

def test_confirm_step_dip_takes_priority_over_cut():
    # onset 很久以前（遠超過 BARGE_CONFIRM_S），但最後發聲在 0.5s 前 → 仍判 dip
    assert confirm_step(onset=0.0, now=100.0, last_spoken=99.5) == "dip"


def test_confirm_step_wait_just_under_threshold():
    assert confirm_step(onset=0.0, now=BARGE_CONFIRM_S - 0.01, last_spoken=BARGE_CONFIRM_S - 0.01) == "wait"


def test_confirm_step_cut_at_threshold():
    assert confirm_step(onset=0.0, now=BARGE_CONFIRM_S, last_spoken=BARGE_CONFIRM_S) == "cut"


def test_confirm_step_dip_at_gap_boundary():
    assert confirm_step(onset=0.0, now=DIP_GAP_S, last_spoken=0.0) == "dip"


def test_confirm_step_wait_just_under_dip_gap():
    assert confirm_step(onset=0.0, now=DIP_GAP_S - 0.01, last_spoken=0.0) == "wait"


# ── user_done ─────────────────────────────────────────────────────────────────

def test_user_done_false_just_under_boundary():
    assert user_done(now=USER_DONE_GAP_S - 0.01, last_spoken=0.0) is False


def test_user_done_true_at_boundary():
    assert user_done(now=USER_DONE_GAP_S, last_spoken=0.0) is True


# ── interrupted_text ──────────────────────────────────────────────────────────

def test_interrupted_text_excludes_lines_outside_window():
    lines = [(0.0, "太久以前"), (50.0, "還在窗內")]
    now = 61.0  # 第一行 61s 前，超過 60s 窗
    assert interrupted_text(lines, now) == "還在窗內"


def test_interrupted_text_truncates_to_max_chars():
    lines = [(0.0, "x" * (INTERRUPTED_TEXT_MAX_CHARS + 50))]
    result = interrupted_text(lines, 0.0)
    assert len(result) == INTERRUPTED_TEXT_MAX_CHARS
    assert result == "x" * INTERRUPTED_TEXT_MAX_CHARS


def test_interrupted_text_empty_when_no_lines():
    assert interrupted_text([], 0.0) == ""


# ── clean_confirm ─────────────────────────────────────────────────────────────

def test_clean_confirm_none_is_empty():
    assert clean_confirm(None) == ""


def test_clean_confirm_empty_string_is_empty():
    assert clean_confirm("") == ""


def test_clean_confirm_lue_is_empty():
    assert clean_confirm("略") == ""


def test_clean_confirm_lue_with_whitespace_is_empty():
    assert clean_confirm("  略 ") == ""


def test_clean_confirm_strips_paired_quotes():
    assert clean_confirm("「喔，你是要問A」") == "喔，你是要問A"


def test_clean_confirm_takes_first_sentence_only():
    assert clean_confirm("喔你是要問A。然後B。") == "喔你是要問A。"


def test_clean_confirm_too_long_is_empty():
    long_text = "喔" * (CONFIRM_MAX_CHARS + 1)
    assert clean_confirm(long_text) == ""


def test_clean_confirm_newline_keeps_only_first_line():
    assert clean_confirm("喔你是要問A\n這是第二行") == "喔你是要問A"

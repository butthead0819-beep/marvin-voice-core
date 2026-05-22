"""T2: commitment → callback producer.

從 SessionSummarizer 偵測到的 commitment（PendingConfirmation）產出主動 callback。
範圍：只處理 inbound（speaker 自己的承諾）→ 提醒本人（你上次說要X），低隱私風險。
outbound（叫別人做的）= 跨人 relay，不在此（deferred）。
純函式，wiring（enqueue_callback + 保留 pending-confirmation）在 voice_controller。
"""
from types import SimpleNamespace
from session_summarizer import commitment_to_callback


def _conf(task_text="戒咖啡", speaker="大肚", direction="inbound"):
    return SimpleNamespace(task_text=task_text, speaker=speaker, direction=direction)


def test_inbound_commitment_produces_self_reminder():
    out = commitment_to_callback(_conf(task_text="戒咖啡", speaker="大肚"))
    assert out == ("大肚", "戒咖啡")


def test_outbound_commitment_skipped():
    # 叫別人做的（跨人）= relay，不在 T2 範圍
    assert commitment_to_callback(_conf(direction="outbound")) is None


def test_empty_task_text_skipped():
    assert commitment_to_callback(_conf(task_text="")) is None
    assert commitment_to_callback(_conf(task_text="   ")) is None


def test_empty_speaker_skipped():
    assert commitment_to_callback(_conf(speaker="")) is None


def test_none_conf_skipped():
    assert commitment_to_callback(None) is None


def test_strips_whitespace():
    assert commitment_to_callback(_conf(task_text="  帶木炭  ", speaker="小明")) == ("小明", "帶木炭")

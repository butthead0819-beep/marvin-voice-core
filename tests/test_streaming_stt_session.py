"""StreamingSTTSession — daemon 管理 + endpointer 整合（Volatile Phase 1）。

把 daemon 的 volatile JSONL 餵進 SemanticEndpointer，斷句決策觸發 on_cut。
subprocess 管理另測（daemon 已有端到端煙霧）；本檔聚焦 line→endpointer→cut
的整合與不變量（每語句只切一次、reset 清狀態、final 兜底）。
"""
from __future__ import annotations

import pytest

from streaming_stt_session import StreamingSTTSession


def _session(**kw):
    cuts = []
    s = StreamingSTTSession(on_cut=lambda text, meta: cuts.append((text, meta)), **kw)
    return s, cuts


def test_volatile_lines_drive_endpointer_to_cut():
    s, cuts = _session(stability_window_ms=400, min_duration_ms=100)
    s.begin_utterance()
    for line in [
        '{"v":"馬文","t_ms":100}',
        '{"v":"馬文播放","t_ms":300}',
        '{"v":"馬文播放晴天","t_ms":500}',
        '{"v":"馬文播放晴天","t_ms":950}',  # 穩定 450ms ≥ 400 → 切
    ]:
        s.on_daemon_line(line)
    assert len(cuts) == 1
    assert cuts[0][0] == "馬文播放晴天"
    assert cuts[0][1]["source"] == "semantic_endpoint"


def test_only_one_cut_per_utterance():
    s, cuts = _session(stability_window_ms=300, min_duration_ms=100)
    s.begin_utterance()
    for line in [
        '{"v":"播歌","t_ms":100}', '{"v":"播歌","t_ms":500}',
        '{"v":"播歌","t_ms":900}', '{"v":"播歌","t_ms":1300}',
    ]:
        s.on_daemon_line(line)
    assert len(cuts) == 1  # 後續穩定更新不重複切


def test_reset_allows_next_utterance_cut():
    s, cuts = _session(stability_window_ms=300, min_duration_ms=100)
    s.begin_utterance()
    s.on_daemon_line('{"v":"第一句","t_ms":100}')
    s.on_daemon_line('{"v":"第一句","t_ms":500}')
    assert len(cuts) == 1
    s.begin_utterance()  # reset
    s.on_daemon_line('{"v":"第二句","t_ms":100}')
    s.on_daemon_line('{"v":"第二句","t_ms":500}')
    assert len(cuts) == 2


def test_final_line_cuts_if_endpoint_never_fired():
    """daemon final（F 收尾）兜底：語意斷句沒先觸發 → final 也要切一次。"""
    s, cuts = _session(stability_window_ms=5000, min_duration_ms=100)  # 窗超大不會自切
    s.begin_utterance()
    s.on_daemon_line('{"v":"馬文","t_ms":100}')
    s.on_daemon_line('{"final":"馬文播放","t_ms":600}')
    assert len(cuts) == 1
    assert cuts[0][0] == "馬文播放"
    assert cuts[0][1]["source"] == "daemon_final"


def test_final_after_endpoint_cut_does_not_double():
    s, cuts = _session(stability_window_ms=300, min_duration_ms=100)
    s.begin_utterance()
    s.on_daemon_line('{"v":"播歌","t_ms":100}')
    s.on_daemon_line('{"v":"播歌","t_ms":500}')      # 語意切
    s.on_daemon_line('{"final":"播歌","t_ms":700}')  # final 不該再切
    assert len(cuts) == 1


def test_malformed_line_ignored():
    s, cuts = _session(stability_window_ms=300, min_duration_ms=100)
    s.begin_utterance()
    s.on_daemon_line("not json")
    s.on_daemon_line('{"ready":true}')   # 啟動訊號非 volatile
    s.on_daemon_line('{"v":"嗨","t_ms":100}')
    assert len(cuts) == 0  # 還沒穩定


def test_cut_meta_carries_revision_count():
    s, cuts = _session(stability_window_ms=400, min_duration_ms=100)
    s.begin_utterance()
    s.on_daemon_line('{"v":"馬聞","t_ms":100}')
    s.on_daemon_line('{"v":"馬文播放","t_ms":300}')  # 改寫
    s.on_daemon_line('{"v":"馬文播放","t_ms":750}')
    assert cuts[0][1]["revision_count"] == 1

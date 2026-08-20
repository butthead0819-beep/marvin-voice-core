"""TDD — 推薦解釋層 Evidence 抽取：music_recommender.extract_evidence /
taste_profile.extract_discover_new_evidence。

從 song.plays[]/requesters（rediscover）與 adjacent_artists（discover_new）抽出
結構化 Evidence，供解釋層槽位填空使用。fail-open：格式異常的候選跳過（回 None）+
log，不中斷推薦流程。
"""
from __future__ import annotations

import logging

from music_recommender import Candidate, Evidence, extract_evidence
from taste_profile import extract_discover_new_evidence


def _candidate(**overrides) -> Candidate:
    defaults = dict(
        anchor_title="晴天",
        anchor_artist="周杰倫",
        lane="long_tail",
        mode="direct",
        target_member="jack",
        score=50.0,
    )
    defaults.update(overrides)
    return Candidate(**defaults)


class TestExtractEvidenceRediscover:
    def test_single_requester_uses_personal_subject(self):
        song = {
            "plays": [{"by": "jack", "ts": 1000.0}, {"by": "jack", "ts": 2000.0}],
            "requesters": {"jack": 2},
        }
        ev = extract_evidence(song, _candidate())
        assert ev is not None
        assert ev.subject == "you"
        assert ev.requester == "jack"
        assert ev.play_count == 2
        assert ev.timestamp == 2000.0
        assert ev.signal_type == "listen"
        assert ev.source_tier == "long_tail"

    def test_multiple_requesters_uses_group_subject(self):
        song = {
            "plays": [{"by": "jack", "ts": 1000.0}, {"by": "suki", "ts": 1500.0}],
            "requesters": {"jack": 1, "suki": 1},
        }
        ev = extract_evidence(song, _candidate())
        assert ev is not None
        assert ev.subject == "you_all"
        assert ev.requester is None

    def test_liked_lane_maps_to_like_signal(self):
        song = {"plays": [], "requesters": {"jack": 1}}
        ev = extract_evidence(song, _candidate(lane="liked"))
        assert ev is not None
        assert ev.signal_type == "like"

    def test_malformed_plays_fails_open_and_logs(self, caplog):
        song = {"plays": "not-a-list", "requesters": {"jack": 1}}
        with caplog.at_level(logging.WARNING):
            ev = extract_evidence(song, _candidate())
        assert ev is None
        assert "evidence 抽取失敗" in caplog.text

    def test_malformed_requesters_fails_open_and_logs(self, caplog):
        song = {"plays": [], "requesters": "not-a-dict"}
        with caplog.at_level(logging.WARNING):
            ev = extract_evidence(song, _candidate())
        assert ev is None
        assert "evidence 抽取失敗" in caplog.text

    def test_missing_fields_fail_open_not_raise(self):
        song = {}
        ev = extract_evidence(song, _candidate())
        assert ev is not None  # 空 plays/requesters 是合法空值，不是格式異常
        assert ev.play_count == 0
        assert ev.timestamp is None


class TestExtractDiscoverNewEvidence:
    def test_artist_in_adjacent_list_returns_evidence(self):
        ev = extract_discover_new_evidence("伍佰", ["伍佰", "陳昇"])
        assert ev is not None
        assert ev.signal_type == "adjacent_artist"
        assert ev.subject == "you"
        assert ev.timestamp is None
        assert ev.play_count == 0

    def test_artist_not_in_adjacent_list_returns_none(self):
        assert extract_discover_new_evidence("五月天", ["伍佰", "陳昇"]) is None

    def test_malformed_adjacent_artists_fails_open_and_logs(self, caplog):
        with caplog.at_level(logging.WARNING):
            ev = extract_discover_new_evidence("伍佰", "not-a-list")
        assert ev is None
        assert "discover_new evidence 抽取失敗" in caplog.text

    def test_empty_artist_returns_none(self):
        assert extract_discover_new_evidence("", ["伍佰"]) is None

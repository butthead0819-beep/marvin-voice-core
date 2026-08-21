"""TDD — 推薦解釋層槽位填空生成：explanation_slotfill.generate_explanation()。

每個槽位值都對 Evidence 做型別檢查後才 render，結構上不可能出現 evidence 之外的
內容；多套句型模板輪替避免同一組合連續兩次選到同一句。
"""
from __future__ import annotations

import time

from explanation_slotfill import TemplateRotationStore, generate_explanation
from music_recommender import Evidence


def _store(tmp_path) -> TemplateRotationStore:
    return TemplateRotationStore(path=str(tmp_path / "explanation_template_state.json"))


def _evidence(**overrides) -> Evidence:
    defaults = dict(
        signal_type="listen",
        timestamp=time.time() - 3 * 7 * 86400.0,  # 3 週前
        play_count=5,
        skip_count=0,
        source_tier="long_tail",
        subject="you",
        requester="jack",
    )
    defaults.update(overrides)
    return Evidence(**defaults)


class TestGenerateExplanation:
    def test_none_evidence_returns_none(self, tmp_path):
        assert generate_explanation(None, store=_store(tmp_path)) is None

    def test_valid_evidence_renders_a_sentence(self, tmp_path):
        text = generate_explanation(_evidence(), store=_store(tmp_path))
        assert isinstance(text, str) and text

    def test_group_subject_uses_you_all_phrasing(self, tmp_path):
        text = generate_explanation(
            _evidence(subject="you_all", requester=None), store=_store(tmp_path)
        )
        assert text is not None
        assert "你們" in text

    def test_adjacent_artist_signal_renders_without_timestamp(self, tmp_path):
        ev = _evidence(signal_type="adjacent_artist", timestamp=None, play_count=0)
        text = generate_explanation(ev, store=_store(tmp_path))
        assert text is not None

    def test_missing_timestamp_skips_templates_requiring_weeks_ago(self, tmp_path):
        # play_count 型別檢查過但 timestamp 沒有 → 只剩不需要 weeks_ago 的模板合格
        ev = _evidence(timestamp=None, play_count=5)
        text = generate_explanation(ev, store=_store(tmp_path))
        assert text is not None
        assert "週前" not in text

    def test_non_int_play_count_excluded_from_slots(self, tmp_path):
        ev = _evidence(play_count="five", timestamp=None)  # 型別不對 → 該槽位不進值
        text = generate_explanation(ev, store=_store(tmp_path))
        # listen/you 唯一不需要 weeks_ago 也不需要 play_count 的模板不存在
        # （三套都至少需要一格），所以應該落回 None（無合適模板，不是拋例外）
        assert text is None

    def test_future_timestamp_excluded_as_negative_weeks(self, tmp_path):
        ev = _evidence(timestamp=time.time() + 86400.0, play_count=None)
        text = generate_explanation(ev, store=_store(tmp_path))
        assert text is None

    def test_unknown_signal_subject_combo_returns_none(self, tmp_path):
        ev = _evidence(signal_type="skip", subject="stranger")
        assert generate_explanation(ev, store=_store(tmp_path)) is None

    def test_rotation_avoids_repeating_same_template_consecutively(self, tmp_path):
        store = _store(tmp_path)
        ev = _evidence()
        first = generate_explanation(ev, store=store)
        second = generate_explanation(ev, store=store)
        assert first != second

    def test_rotation_persists_across_store_instances(self, tmp_path):
        path = str(tmp_path / "explanation_template_state.json")
        store1 = TemplateRotationStore(path=path)
        first = generate_explanation(_evidence(), store=store1)

        store2 = TemplateRotationStore(path=path)
        second = generate_explanation(_evidence(), store=store2)

        assert first != second

    def test_like_signal_you_subject_renders(self, tmp_path):
        ev = _evidence(signal_type="like")
        text = generate_explanation(ev, store=_store(tmp_path))
        assert text is not None

    def test_radio_related_renders_seed_title(self, tmp_path):
        ev = _evidence(
            signal_type="radio_related", subject="you_all", requester=None,
            timestamp=None, play_count=0, seed_title="晴天",
        )
        text = generate_explanation(ev, store=_store(tmp_path))
        assert text is not None
        assert "晴天" in text

    def test_radio_related_without_seed_title_skips_template(self, tmp_path):
        ev = _evidence(
            signal_type="radio_related", subject="you_all", requester=None,
            timestamp=None, play_count=0, seed_title=None,
        )
        assert generate_explanation(ev, store=_store(tmp_path)) is None

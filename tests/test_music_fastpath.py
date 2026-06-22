"""MusicFastPath：糊字點歌 → 拼音 fuzzy 比對乾淨 canonical 歌表。

驗證來源：scripts/music_homophone_harness.py 在乾淨 canonical 上的實測
（同音字命中、英文名邊界、拒絕案例 <80）。pypinyin/rapidfuzz 缺則 skip。
"""
import json

import pytest

pytest.importorskip("rapidfuzz")
pytest.importorskip("pypinyin")

from music_fastpath import MusicFastPath  # noqa: E402

# 乾淨 canonical（ytmusicapi/排行榜風格「歌手 歌名」）+ decoy 湊規模
_CATALOG = [
    "周杰倫 七里香", "周杰倫 晴天", "周杰倫 稻香", "周杰倫 龍捲風", "周杰倫 屋頂",
    "關喆 想你的夜", "陶喆 月亮代表誰的心", "陶喆 Susan說", "陶喆 流沙",
    "張惠妹 如果你也聽說", "信樂團 離歌", "曲婉婷 我的歌聲裡", "莫文蔚 慢慢喜歡你",
    "Beyond 海闊天空", "齊秦 火柴天堂", "鄧紫棋 泡沫", "五月天 倔強",
]


@pytest.fixture
def fp(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps([{"name": n} for n in _CATALOG], ensure_ascii=False),
                    encoding="utf-8")
    return MusicFastPath(catalog_path=path, threshold=80)


def test_clean_query_matches_canonical(fp):
    name, score = fp.match("七里香")
    assert name == "周杰倫 七里香"
    assert score >= 80


def test_homophone_garble_matches_via_pinyin(fp):
    # 官者→關喆（guan zhe 同音）；字元比對救不回，拼音救回
    name, score = fp.match("官者的想你的夜")
    assert name == "關喆 想你的夜"
    assert score >= 80


def test_homophone_partial_matches(fp):
    # 月亮錶→月亮代表
    name, _ = fp.match("陶喆的月亮錶是誰的心")
    assert name == "陶喆 月亮代表誰的心"


def test_nonsense_query_rejected(fp):
    assert fp.match("亂碼歌zzz完全不存在") is None


def test_non_song_chitchat_rejected(fp):
    assert fp.match("今天天氣真好啊") is None


def test_empty_query_returns_none(fp):
    assert fp.match("") is None
    assert fp.match("   ") is None


def test_missing_catalog_disables_fastpath(tmp_path):
    fp = MusicFastPath(catalog_path=tmp_path / "nope.json", threshold=80)
    assert fp.enabled is False
    assert fp.match("七里香") is None


def test_voice_controller_hook_gated_off_by_default(monkeypatch):
    """安全不變量：MARVIN_MUSIC_FASTPATH 未設 → hook 回 None → 不改 cleaner 行為。"""
    from types import SimpleNamespace
    from cogs.voice_controller import VoiceController

    monkeypatch.delenv("MARVIN_MUSIC_FASTPATH", raising=False)
    assert VoiceController._get_music_fastpath(SimpleNamespace()) is None

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
    "盧廣仲 輕輕對你說", "陳華 想和你看五月的晚霞",  # 退化歌名 / 多「的」歌名 回歸用例
]


@pytest.fixture
def fp(tmp_path):
    path = tmp_path / "catalog.json"
    rows = [{"name": n, "videoId": f"vid_{i:03d}"} for i, n in enumerate(_CATALOG)]
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return MusicFastPath(catalog_path=path, threshold=80)


def test_clean_query_matches_canonical(fp):
    name, score, _ = fp.match("七里香")
    assert name == "周杰倫 七里香"
    assert score >= 80


def test_command_prefix_stripped(fp):
    """真實點歌 query 帶命令前綴「播放/放/點播」→ 要剝掉只比歌名，否則覆蓋率守門誤擋。
    （2026-06-22：fast-path 零命中真因——所有真實 query 帶播放前綴被守門擋掉。）"""
    for q in ["播放周杰倫的七里香", "放七里香", "點播周杰倫七里香", "幫我播放七里香"]:
        r = fp.match(q)
        assert r is not None and "七里香" in r[0], f"{q} 應命中"


def test_homophone_garble_matches_via_pinyin(fp):
    # 官者→關喆（guan zhe 同音）；字元比對救不回，拼音救回
    name, score, _ = fp.match("官者的想你的夜")
    assert name == "關喆 想你的夜"
    assert score >= 80


def test_homophone_partial_matches(fp):
    # 月亮錶→月亮代表
    name, _, _vid = fp.match("陶喆的月亮錶是誰的心")
    assert name == "陶喆 月亮代表誰的心"


def test_nonsense_query_rejected(fp):
    assert fp.match("亂碼歌zzz完全不存在") is None


def test_non_song_chitchat_rejected(fp):
    assert fp.match("今天天氣真好啊") is None


def test_known_artist_wrong_song_rejected(fp):
    """防「藝人對、歌錯」：點某藝人但歌名不在庫 → 不該配到同藝人別首（覆蓋率守門）。
    周杰倫在庫但「不存在歌名」非任何在庫曲 → 應 None 走 cleaner，而非播別首周杰倫。"""
    assert fp.match("周杰倫的這首歌不存在啦啦啦") is None
    assert fp.match("周杰倫的隨便亂掰一個") is None


def test_artist_right_degenerate_song_rejected(fp):
    """防「藝人對、歌名退化」假命中（2026-06-23 live：盧廣仲的對啊對啊→輕輕對你說 88 假過）。
    「對啊對啊」去 stopword「啊」只剩單一 token dui、又剛好在別首「對你說」→ 舊覆蓋率被藝人
    名灌到 4/4 假過關。歌名(的後)content token <2 → 退化 query，應 None 走 cleaner。"""
    assert fp.match("盧廣仲的對啊對啊") is None
    assert fp.match("播放盧廣仲的對啊對啊") is None


def test_multi_de_song_title_still_matches(fp):
    """歌名本身含「的」（五月『的』晚霞）→「的」切藝人只能切第一個，歌名 token 仍充足、照命中。
    （陪你看→想和你看 同音/近義糊字，靠拼音+藝人撐分。）"""
    hit = fp.match("陳華的陪你看五月的晚霞")
    assert hit is not None and hit[0] == "陳華 想和你看五月的晚霞"


def test_fastpath_output_dispatchable_as_play(fp):
    """回歸：fast-path canonical 必須被 music agent 認成點歌（strong_play, 無 missing），
    否則裸「藝人 歌名」無動詞 → bus bid 0.00 drop → 不播 / Marvin 幻覺「已為你播放」
    （2026-06-23 18:33 incident：陳華晚霞 conf=0.00 drop→幻覺）。
    to_play_command 補「放一首」前綴 → strong_play 0.95；播放時動詞被 _extract 剝掉。"""
    from music_fastpath import to_play_command
    from intent_agents.music_agent_v2 import MusicAgentV2
    from intent_bus import IntentContext

    hit = fp.match("陳華的陪你看五月的晚霞")
    canonical = hit[0]

    class _C:
        _STRONG_PLAY_KW = ["放音樂", "播音樂", "放首歌", "播首歌", "放一首", "播一首",
                           "來首", "搜尋歌曲"]
        _WEAK_PLAY_KW = ["播放", "我想聽", "放點", "播點", "幫我找", "幫我放"]
        _MUSIC_SKIP_KW = ["換一首"]; _MUSIC_STOP_KW = ["停止播放"]
        _MUSIC_PAUSE_KW = ["暫停音樂"]; _MUSIC_RESUME_KW = ["繼續播"]
        async def _safe_music_command(self, *a, **k): pass
        async def _ask_music_followup(self, *a, **k): pass

    def _ctx(q):
        return IntentContext(speaker="x", raw_text=q, query=q, original_raw=q,
                             wake_intent=0.9, stream_active=False, game_mode=False,
                             is_owner=False, now=0.0)

    agent = MusicAgentV2(_C())
    assert agent.bid(_ctx(canonical)).confidence < 0.30          # 裸 canonical → bus drop（bug）
    b = agent.bid(_ctx(to_play_command(canonical)))              # 補前綴 → 認得
    assert b.confidence >= 0.95 and b.missing_slots == []        # strong_play、直接播不追問


def test_empty_query_returns_none(fp):
    assert fp.match("") is None
    assert fp.match("   ") is None


def test_playlist_command_phrases_not_hijacked(tmp_path):
    """personal_shuffle 觸發詞（我的歌單/我點過的歌/個人歌單…）不是具體歌名，fast-path
    不該攔截——否則改寫成歌名/URL → personal_shuffle 看不到觸發詞 → 永遠贏不了。
    2026-06-30 live bug：『我的歌單』拼音 token(wo/ge/dan)散落命中長標題『茄子蛋 愛情你
    比我想的閣較偉大』token_set 100 假命中、劫走 personal_shuffle（播我的歌單沒反應）。"""
    path = tmp_path / "c.json"
    path.write_text(json.dumps([
        {"name": "茄子蛋 愛情你比我想的閣較偉大", "videoId": "vNloGR7mF1Y"},
        {"name": "曲婉婷 我的歌聲裡", "videoId": "x1"},
        {"name": "周杰倫 七里香", "videoId": "y1"},
    ], ensure_ascii=False), encoding="utf-8")
    fp = MusicFastPath(catalog_path=path, threshold=80)
    for q in ["我的歌單", "播我的歌單", "馬文播放我的歌單", "隨機播我點過的歌", "個人歌單"]:
        assert fp.match(q) is None, f"'{q}' 應交給 personal_shuffle，fast-path 不該攔截"
    assert fp.match("播放七里香") is not None  # 真歌名仍命中（排除清單不誤傷）


def test_catalog_hot_reload_on_mtime_change(tmp_path):
    """目錄檔更新（3am cron 重建）→ 不重啟也熱重載吃到新歌。"""
    import os

    path = tmp_path / "catalog.json"
    path.write_text(json.dumps([{"name": "周杰倫 七里香"}], ensure_ascii=False),
                    encoding="utf-8")
    fp = MusicFastPath(catalog_path=path, threshold=80)
    assert fp.match("七里香") is not None
    assert fp.match("蔡依林的倒帶") is None  # 還沒在庫

    # 重建目錄（加新歌），強制 mtime 變
    path.write_text(json.dumps([{"name": "周杰倫 七里香"}, {"name": "蔡依林 倒帶"}],
                               ensure_ascii=False), encoding="utf-8")
    os.utime(path, (fp._mtime + 100, fp._mtime + 100))

    hit = fp.match("蔡依林的倒帶")  # 熱重載後應命中
    assert hit is not None and "倒帶" in hit[0]


def test_fastpath_play_query_hit_builds_play_command(fp):
    """命中 → 回 to_play_command 包出的指令（帶前綴 + watch?v= videoId）。"""
    from music_fastpath import fastpath_play_query, FASTPATH_PLAY_PREFIX
    result = fastpath_play_query(fp, "七里香")
    assert isinstance(result, str)
    assert result.startswith(FASTPATH_PLAY_PREFIX)
    assert "watch?v=" in result  # vid_000 在 catalog 第 0 筆


def test_fastpath_play_query_miss_returns_input_unchanged(fp):
    """未命中（閒聊句）→ 原樣回傳。"""
    from music_fastpath import fastpath_play_query
    q = "今天天氣真好啊"
    assert fastpath_play_query(fp, q) == q


def test_fastpath_play_query_none_fp_returns_input():
    """fp=None → 原樣回傳，不 crash。"""
    from music_fastpath import fastpath_play_query
    q = "七里香"
    assert fastpath_play_query(None, q) == q


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


def test_match_returns_video_id_as_third_element(fp):
    """match() 命中時，第 3 個元素等於 catalog row 的 videoId。"""
    # 周杰倫 七里香 是 _CATALOG 第 0 筆 → videoId = vid_000
    result = fp.match("七里香")
    assert result is not None
    name, score, video_id = result
    assert name == "周杰倫 七里香"
    assert video_id == "vid_000"


def test_to_play_command_with_video_id_builds_watch_url():
    """to_play_command(canonical, video_id) 有 videoId → 回 watch URL 指令。"""
    from music_fastpath import to_play_command, FASTPATH_PLAY_PREFIX
    cmd = to_play_command("周杰倫 七里香", "dQw4w9WgXcQ")
    assert cmd.startswith(FASTPATH_PLAY_PREFIX)
    assert "watch?v=dQw4w9WgXcQ" in cmd


def test_to_play_command_without_video_id_returns_name_command():
    """to_play_command(canonical) 沒有 videoId → 回原本的歌名指令。"""
    from music_fastpath import to_play_command, FASTPATH_PLAY_PREFIX
    cmd = to_play_command("周杰倫 七里香")
    assert cmd == f"{FASTPATH_PLAY_PREFIX}周杰倫 七里香"
    assert "watch?v=" not in cmd

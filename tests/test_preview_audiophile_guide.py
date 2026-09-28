"""TDD：scripts/preview_audiophile_guide.py（docs/PLAN_audiophile_music_tour.md Phase 5.1）。

離線眼驗導聆稿 / 巡禮曲目，不上台。護欄：
  - 不寫正本快取 records/song_knowledge.json（bot 24/7 同檔整份寫，會互蓋）——
    讀正本的**暫存複本**（快取命中照樣看得到），寫只寫進複本
  - 預設只走免費 key；--allow-paid 才帶付費 client（付費照樣過 PaidUsageGuard 記帳）
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

GUIDE = "別被雙截棍的快嘴騙了，" + "仔細聽左聲道那把二胡，" * 6 + "戴上耳機。"


def _resp(text, *, chunks=1):
    chunk_objs = [SimpleNamespace(web=SimpleNamespace(uri="https://music.example.com/a"))
                  for _ in range(chunks)]
    cand = SimpleNamespace(grounding_metadata=SimpleNamespace(grounding_chunks=chunk_objs),
                           finish_reason="STOP")
    return SimpleNamespace(text=text, candidates=[cand],
                           usage_metadata=SimpleNamespace(prompt_token_count=1, candidates_token_count=1))


def _client(resp=None, exc=None):
    cli = MagicMock()
    cli.aio.models.generate_content = (AsyncMock(side_effect=exc) if exc
                                       else AsyncMock(return_value=resp))
    return cli


def _args(argv):
    from scripts.preview_audiophile_guide import build_parser
    return build_parser().parse_args(argv)


async def _run(argv, tmp_path, *, free=None, tts_path="/tmp/g.mp3", dur=19.8, live=None):
    from scripts.preview_audiophile_guide import run
    live_path = tmp_path / "song_knowledge.json"
    if live is not None:
        live_path.write_text(json.dumps(live, ensure_ascii=False), encoding="utf-8")
    lines = []
    tts = MagicMock()
    tts.generate_audio = AsyncMock(return_value=tts_path)
    probe = AsyncMock(return_value=dur)
    code = await run(
        _args(argv),
        free_client=free if free is not None else _client(_resp(GUIDE)),
        paid_client=None, guard=MagicMock(),
        tts_engine=tts, probe_duration=probe,
        live_cache_path=str(live_path), out=lines.append,
    )
    return code, "\n".join(str(x) for x in lines), tts, live_path


@pytest.mark.asyncio
async def test_song_mode_prints_guide_sources_length_and_tts(tmp_path):
    code, out, tts, _ = await _run(["--artist", "周杰倫", "--song", "雙截棍"], tmp_path)
    assert code == 0
    assert GUIDE in out
    assert "music.example.com" in out          # 來源
    assert str(len(GUIDE)) in out              # 字數
    assert "/tmp/g.mp3" in out and "19.8" in out  # TTS 檔案 + 精確秒數
    tts.generate_audio.assert_awaited_once_with(GUIDE)


@pytest.mark.asyncio
async def test_song_mode_never_writes_live_cache(tmp_path):
    live = {"周杰倫 - 雙截棍": {"lyricist": "方文山"}}
    _, _, _, live_path = await _run(["--artist", "周杰倫", "--song", "雙截棍"], tmp_path, live=live)
    assert json.loads(live_path.read_text(encoding="utf-8")) == live


@pytest.mark.asyncio
async def test_song_mode_reads_live_cache_hits_via_copy(tmp_path):
    live = {"audiophile::周杰倫 - 雙截棍": {"audiophile_guide": "快取裡的稿", "sources": ["a.com"]}}
    free = _client(_resp("不該被呼叫"))
    code, out, _, _ = await _run(["--artist", "周杰倫", "--song", "雙截棍"], tmp_path, free=free, live=live)
    assert code == 0
    assert "快取裡的稿" in out
    assert "快取命中" in out
    free.aio.models.generate_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_song_mode_flags_fallback(tmp_path):
    code, out, _, _ = await _run(["--artist", "周杰倫", "--song", "雙截棍"], tmp_path,
                                 free=_client(exc=RuntimeError("boom")))
    assert "保底" in out
    assert code == 1


@pytest.mark.asyncio
async def test_no_tts_flag_skips_rendering(tmp_path):
    _, out, tts, _ = await _run(["--artist", "周杰倫", "--song", "雙截棍", "--no-tts"], tmp_path)
    tts.generate_audio.assert_not_awaited()
    assert GUIDE in out


@pytest.mark.asyncio
async def test_album_mode_prints_tracklist(tmp_path):
    free = _client(_resp("1. 愛在西元前\n2. 簡單愛"))
    code, out, tts, _ = await _run(["--artist", "周杰倫", "--album", "范特西"], tmp_path, free=free)
    assert code == 0
    assert "1. 愛在西元前" in out and "2. 簡單愛" in out
    assert "music.example.com" in out
    tts.generate_audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_album_mode_not_found_exit_1(tmp_path):
    code, out, _, _ = await _run(["--artist", "周杰倫", "--album", "不存在"], tmp_path,
                                 free=_client(_resp("無")))
    assert code == 1
    assert "查不到" in out


def test_parser_requires_song_or_album_and_paid_off_by_default():
    args = _args(["--artist", "周杰倫", "--song", "雙截棍"])
    assert args.allow_paid is False
    with pytest.raises(SystemExit):
        _args(["--artist", "周杰倫"])
    with pytest.raises(SystemExit):
        _args(["--artist", "周杰倫", "--song", "a", "--album", "b"])

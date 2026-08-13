"""
tests/test_car_open.py
TDD：車載開場時段解析（ESP32 puck 讀空氣開場的地基）。

5 個 bucket + 跨午夜（design doc / eng review）：
  morning     05–11
  noon        11–14
  afternoon   14–18
  evening     18–23
  late_night  23–05（跨午夜 wrap）
純函式、datetime 當參數傳（零 now() 依賴，可測）。
"""
import datetime as _dt
import pytest


def _at(hour):
    return _dt.datetime(2026, 7, 14, hour, 30, 0)


@pytest.mark.parametrize("hour,expected", [
    (5, "morning"), (7, "morning"), (10, "morning"),
    (11, "noon"), (12, "noon"), (13, "noon"),
    (14, "afternoon"), (16, "afternoon"), (17, "afternoon"),
    (18, "evening"), (20, "evening"), (22, "evening"),
    (23, "late_night"), (0, "late_night"), (3, "late_night"), (4, "late_night"),
])
def test_resolve_time_bucket_boundaries(hour, expected):
    from car_open import resolve_time_bucket
    assert resolve_time_bucket(_at(hour)) == expected


def test_resolve_time_bucket_returns_known_bucket_only():
    from car_open import resolve_time_bucket, TIME_BUCKETS
    for h in range(24):
        assert resolve_time_bucket(_at(h)) in TIME_BUCKETS


def test_midnight_wrap_late_night():
    """跨午夜：23:xx 與 00:xx–04:xx 同屬 late_night。"""
    from car_open import resolve_time_bucket
    assert resolve_time_bucket(_at(23)) == "late_night"
    assert resolve_time_bucket(_dt.datetime(2026, 7, 14, 0, 5)) == "late_night"
    assert resolve_time_bucket(_dt.datetime(2026, 7, 15, 4, 59)) == "late_night"
    # 05:00 整已跳出 late_night
    assert resolve_time_bucket(_dt.datetime(2026, 7, 15, 5, 0)) == "morning"


# ── build_car_open：復用 pick_candidate + 預生成開場白，絕不付費 LLM ──────────
def _cand(title, score=1.0):
    from music_recommender import Candidate
    return Candidate(anchor_title=title, anchor_artist="x", lane="long_tail",
                     mode="direct", target_member=None, score=score)


def test_build_car_open_picks_line_and_reuses_pick_candidate():
    from car_open import build_car_open
    pool = [_cand("晴天"), _cand("稻香")]
    out = build_car_open("morning", pool_provider=lambda: pool,
                         open_lines={"morning": ["早安，來首歌"]})
    assert out.line == "早安，來首歌"
    assert out.song is not None and out.song.anchor_title in ("晴天", "稻香")


def test_build_car_open_empty_pool_song_none_but_still_has_line():
    from car_open import build_car_open
    out = build_car_open("noon", pool_provider=lambda: [],
                         open_lines={"noon": ["午安"]})
    assert out.song is None          # 沒候選→不硬湊，caller 決定降級
    assert out.line == "午安"


def test_build_car_open_missing_bucket_uses_fallback_line():
    from car_open import build_car_open, _FALLBACK_OPEN_LINE
    out = build_car_open("evening", pool_provider=lambda: [], open_lines={})  # 沒 evening
    assert out.line == _FALLBACK_OPEN_LINE


def test_build_car_open_never_calls_paid_llm(monkeypatch):
    """付費鐵則守門：開場路徑絕不觸發 call_paid_review。"""
    import llm_pool

    def _boom(*a, **k):
        raise AssertionError("開場不准打付費 LLM")

    monkeypatch.setattr(llm_pool, "call_paid_review", _boom, raising=False)
    from car_open import build_car_open
    out = build_car_open("morning", pool_provider=lambda: [_cand("晴天")],
                         open_lines={"morning": ["早安"]})
    assert out.song is not None      # 順利選到，且沒炸＝沒打付費


# ── resolve_car_open_query：非單曲品質閘 + 候選池重試（2026-08-13 review 補測） ──
_LONG_DURATION = 901   # > track_quality.MAX_SONG_DURATION_S(900)


def _info(title, duration=180, webpage_url=None):
    return {"title": title, "duration": duration, "webpage_url": webpage_url}


@pytest.mark.asyncio
async def test_resolve_car_open_query_returns_webpage_url_when_gate_passes():
    from car_open import resolve_car_open_query

    async def _resolve(q):
        return _info("正常單曲", duration=200, webpage_url="https://youtu.be/abc")

    out = await resolve_car_open_query(
        _cand("晴天"), pool_provider=lambda: [], resolve_fn=_resolve)
    assert out == "https://youtu.be/abc"


@pytest.mark.asyncio
async def test_resolve_car_open_query_retries_pool_when_first_candidate_fails_gate():
    """第一首是長合輯被閘掉 → 從候選池補抽，第二首正常過閘。"""
    from car_open import resolve_car_open_query

    calls = []

    async def _resolve(q):
        calls.append(q)
        if len(calls) == 1:
            return _info("有聲書合輯", duration=_LONG_DURATION)
        return _info("正常單曲", duration=200, webpage_url="https://youtu.be/second")

    out = await resolve_car_open_query(
        _cand("有聲書"),
        pool_provider=lambda: [_cand("有聲書", score=1.0), _cand("備用曲", score=0.9)],
        resolve_fn=_resolve)
    assert out == "https://youtu.be/second"
    assert len(calls) == 2   # 真的試了第二首，沒有一失敗就放棄


@pytest.mark.asyncio
async def test_resolve_car_open_query_returns_none_when_pool_exhausted():
    """候選池全部都是長合輯 → 最終放棄，回 None（caller 決定開場靜音，不硬播爛片）。"""
    from car_open import resolve_car_open_query

    async def _resolve(q):
        return _info("有聲書", duration=_LONG_DURATION)

    out = await resolve_car_open_query(
        _cand("有聲書A"),
        pool_provider=lambda: [_cand("有聲書A"), _cand("有聲書B"), _cand("有聲書C")],
        resolve_fn=_resolve)
    assert out is None


@pytest.mark.asyncio
async def test_resolve_car_open_query_falls_back_to_raw_query_when_resolve_fails():
    """resolve 掛掉（逾時/MusicCog不可用等）→ 保守放行，回原始 query 讓 caller 照樣嘗試播放。"""
    from car_open import resolve_car_open_query

    async def _resolve(q):
        raise RuntimeError("music_cog_unavailable")

    out = await resolve_car_open_query(
        _cand("晴天", score=1.0), pool_provider=lambda: [], resolve_fn=_resolve)
    assert out == "x 晴天"   # anchor_artist="x" + anchor_title（_cand fixture 預設）


@pytest.mark.asyncio
async def test_resolve_car_open_query_none_song_returns_none():
    from car_open import resolve_car_open_query

    async def _resolve(q):
        raise AssertionError("song=None 不該打 resolve")

    out = await resolve_car_open_query(None, pool_provider=lambda: [], resolve_fn=_resolve)
    assert out is None

"""Play-First：串流出聲不被 meta（歌詞/評論/DJ）生成阻塞。

使用者選擇：「先播音樂，meta 阻塞就放棄 TTS」。故串流迴圈只用「已就緒」的 prefetch
meta；未就緒不等、本首放棄 DJ、meta 背景補。核心決策抽成 _ready_meta 可測。
"""
import asyncio
from unittest.mock import MagicMock

from cogs.music_cog import MusicCog


class _FakeTask:
    def __init__(self, done, result=None, exc=None, cancelled=False):
        self._done = done
        self._result = result
        self._exc = exc
        self._cancelled = cancelled

    def done(self):
        return self._done

    def cancelled(self):
        return self._cancelled

    def result(self):
        if self._cancelled:
            raise asyncio.CancelledError
        if self._exc:
            raise self._exc
        return self._result


def test_ready_when_task_done_with_dict():
    meta = {"lyrics": "x", "comment": "y", "dj": {"text": "z"}}
    assert MusicCog._ready_meta(_FakeTask(done=True, result=meta)) == meta


def test_none_when_task_pending():
    # 未就緒 → None（play-first：不等它，先出聲）
    assert MusicCog._ready_meta(_FakeTask(done=False, result={"lyrics": "x"})) is None


def test_none_when_no_task():
    assert MusicCog._ready_meta(None) is None


def test_none_when_task_failed():
    assert MusicCog._ready_meta(_FakeTask(done=True, exc=RuntimeError("boom"))) is None


def test_none_when_result_not_dict():
    assert MusicCog._ready_meta(_FakeTask(done=True, result="not-a-dict")) is None


def test_none_when_task_cancelled_does_not_raise():
    # prefetch task 被 cancel（CancelledError 是 BaseException）→ 回 None，
    # 絕不能竄出去害 _stream_loop 誤判「迴圈被停」整條佇列收攤（2026-09-02）。
    assert MusicCog._ready_meta(_FakeTask(done=True, cancelled=True)) is None

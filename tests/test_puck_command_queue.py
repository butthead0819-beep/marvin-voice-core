import asyncio

import pytest

from marvin_voice_core.puck_command_queue import (
    PuckCommandQueue,
    PuckCommandQueueClient,
    register_voice_clip,
    resolve_voice_clip,
)


def test_since_zero_returns_all_pushed_commands():
    q = PuckCommandQueue()
    q.play("https://youtu.be/a")
    q.queue_next("https://youtu.be/b")

    seq, pending = q.since(0)

    assert seq == 2
    assert [c["cmd"] for c in pending] == ["play", "queue_next"]
    assert pending[0]["url"] == "https://youtu.be/a"
    assert pending[1]["url"] == "https://youtu.be/b"


def test_since_only_returns_commands_after_given_seq():
    q = PuckCommandQueue()
    q.play("https://youtu.be/a")
    first_seq, _ = q.since(0)
    q.crossfade(duration_s=5.0)

    seq, pending = q.since(first_seq)

    assert seq == 2
    assert len(pending) == 1
    assert pending[0]["cmd"] == "crossfade"
    assert pending[0]["duration_s"] == 5.0


def test_since_with_no_new_commands_returns_empty_list():
    q = PuckCommandQueue()
    q.play("https://youtu.be/a")
    seq, _ = q.since(0)

    seq2, pending = q.since(seq)

    assert seq2 == seq
    assert pending == []


def test_stop_pushes_bare_command():
    q = PuckCommandQueue()
    q.stop()

    seq, pending = q.since(0)

    assert seq == 1
    assert pending == [{"seq": 1, "cmd": "stop"}]


def test_history_trimmed_but_since_never_raises_and_returns_available_history():
    q = PuckCommandQueue(max_history=3)
    for i in range(5):
        q.play(f"https://youtu.be/{i}")

    seq, pending = q.since(0)

    assert seq == 5
    # 舊指令被砍掉，since(0) 拿不回全部 5 筆，但至少要拿到還留著的那些，不能整包消失。
    assert len(pending) == 3
    assert pending[-1]["url"] == "https://youtu.be/4"


# ── speak/sfx（Phase3：DJ口白/SFX） ──────────────────────────────────────────

def test_speak_and_sfx_push_distinct_cmds():
    q = PuckCommandQueue()
    q.speak("clip-1")
    q.sfx("clip-2")

    seq, pending = q.since(0)

    assert seq == 2
    assert pending[0] == {"seq": 1, "cmd": "speak", "clip_id": "clip-1"}
    assert pending[1] == {"seq": 2, "cmd": "sfx", "clip_id": "clip-2"}


# ── voice clip 登記表 ─────────────────────────────────────────────────────

def test_register_voice_clip_roundtrips_to_same_path():
    clip_id = register_voice_clip("/tmp/dj_line_123.opus")
    assert resolve_voice_clip(clip_id) == "/tmp/dj_line_123.opus"


def test_resolve_voice_clip_unknown_id_returns_none():
    assert resolve_voice_clip("no-such-clip-id-ever") is None


def test_register_voice_clip_ids_are_distinct_per_call():
    id_a = register_voice_clip("/tmp/a.opus")
    id_b = register_voice_clip("/tmp/b.opus")
    assert id_a != id_b
    assert resolve_voice_clip(id_a) == "/tmp/a.opus"
    assert resolve_voice_clip(id_b) == "/tmp/b.opus"


# ── PuckCommandQueueClient.speak/sfx（呼叫端不用知道 clip_id 登記細節） ───────

@pytest.mark.asyncio
async def test_client_speak_registers_clip_and_pushes_speak_cmd():
    q = PuckCommandQueue()
    client = PuckCommandQueueClient(q)

    ok = await client.speak("/tmp/dj_speak.opus")

    assert ok is True
    seq, pending = q.since(0)
    assert pending[0]["cmd"] == "speak"
    clip_id = pending[0]["clip_id"]
    assert resolve_voice_clip(clip_id) == "/tmp/dj_speak.opus"


@pytest.mark.asyncio
async def test_client_sfx_registers_clip_and_pushes_sfx_cmd():
    q = PuckCommandQueue()
    client = PuckCommandQueueClient(q)

    ok = await client.sfx("/tmp/scratch.wav")

    assert ok is True
    seq, pending = q.since(0)
    assert pending[0]["cmd"] == "sfx"
    clip_id = pending[0]["clip_id"]
    assert resolve_voice_clip(clip_id) == "/tmp/scratch.wav"

from marvin_voice_core.puck_command_queue import PuckCommandQueue


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

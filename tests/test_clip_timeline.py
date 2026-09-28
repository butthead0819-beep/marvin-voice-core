import json
import sqlite3
import struct
import wave
from datetime import datetime

import pytest

from scripts.clip_timeline import (
    ack_text,
    collect_events,
    find_ack_path,
    format_timeline,
    main,
    parse_bot_log_acks,
    render_review_md,
    validate_candidates,
)


def test_ack_text_and_find_ack_path():
    assert ack_text("music_ack_03.mp3") == "這首好聽"
    assert find_ack_path("music_ack_03.mp3") == "assets/acks/music/music_ack_03.mp3"
    assert ack_text("does_not_exist.mp3") is None
    assert find_ack_path("does_not_exist.mp3") is None


def test_parse_bot_log_acks():
    lines = [
        "2026-09-23 21:24:33,718 [INFO] cogs.voice_controller: 🗣️ [Ack:music] 播放 music_ack_03.mp3",
        "2026-09-23 21:24:40,000 [INFO] cogs.voice_controller: 🗣️ [Ack:status] 播放 idle_short",
        "2026-09-23 21:24:41,000 [INFO] something.other: 無關的一行 log",
    ]
    result = parse_bot_log_acks(lines)
    assert len(result) == 1
    assert result[0]["file"] == "music_ack_03.mp3"
    expected_start = datetime(2026, 9, 23, 21, 24, 33).timestamp() + 0.718
    assert result[0]["start"] == pytest.approx(expected_start, abs=0.001)


def test_collect_events():
    human_rows = [("狗與露", "哈哈哈哈哈哈哈哈哈", 1010.0)]
    speech_rows = [
        {"start": 1015.0, "layer": 1, "voice": None, "src": "tts", "text": "嗨"},
        {"start": 1020.0, "layer": 1, "voice": None, "src": "dj", "text": "今天天氣不錯"},
        {
            "start": 1030.0,
            "layer": 1,
            "voice": None,
            "src": "ack",
            "text": "這首好聽",
            "file": "assets/acks/music/music_ack_03.mp3",
        },
    ]
    bot_acks = [
        {"start": 1031.0, "file": "music_ack_03.mp3"},  # 同檔名、差 1 秒 → 去重
        {"start": 1040.0, "file": "music_ack_17.mp3"},  # 不同檔名 → 保留
    ]

    events = collect_events(human_rows, speech_rows, bot_acks, rec_start=1000.0, duration=60.0)

    assert len(events) == 5
    assert [e["from"] for e in events] == [8.0, 15.0, 20.0, 30.0, 40.0]

    assert events[0]["kind"] == "human"
    assert events[0]["speaker"] == "狗與露"

    assert events[1]["kind"] == "marvin"
    assert events[1]["src"] == "tts"
    assert events[1]["voice"] is None

    assert events[2]["kind"] == "marvin"
    assert events[2]["src"] == "dj"

    assert events[3]["kind"] == "ack"
    assert events[3]["file"] == "assets/acks/music/music_ack_03.mp3"

    assert events[4]["kind"] == "ack"
    assert events[4]["file"] == "assets/acks/music/music_ack_17.mp3"
    assert events[4]["text"] == "找到了"


def test_collect_events_song_card():
    speech_rows = [
        {"start": 1050.0, "src": "song", "text": "七里香", "artist": "周杰倫"},
        {"start": 1200.0, "src": "song", "text": "超出範圍"},  # duration=60 → 過濾
    ]

    events = collect_events([], speech_rows, [], rec_start=1000.0, duration=60.0)

    assert len(events) == 1
    assert events[0] == {
        "kind": "song_card", "from": 50.0, "to": 54.0, "title": "七里香", "artist": "周杰倫",
    }


def test_format_timeline_song_card():
    events = [
        {"kind": "song_card", "from": 50.0, "to": 54.0, "title": "七里香", "artist": "周杰倫"},
    ]
    out = format_timeline(events, rec_start_str="2026-09-23 21:00:00", duration=60.0)
    assert "00:50.0  歌曲  🎵《七里香》— 周杰倫\n" in out


def test_format_timeline_song_card_no_artist():
    events = [
        {"kind": "song_card", "from": 50.0, "to": 54.0, "title": "七里香", "artist": None},
    ]
    out = format_timeline(events, rec_start_str="2026-09-23 21:00:00", duration=60.0)
    assert "00:50.0  歌曲  🎵《七里香》\n" in out
    assert "— " not in out


def test_format_timeline():
    events = [
        {"kind": "human", "speaker": "狗與露", "from": 8.0, "to": 10.8, "text": "哈哈哈哈哈哈哈哈哈"},
        {"kind": "marvin", "voice": None, "from": 20.0, "text": "今天天氣不錯", "src": "dj"},
    ]
    out = format_timeline(events, rec_start_str="2026-09-23 21:00:00", duration=60.0)
    expected = (
        "# 錄音開始 2026-09-23 21:00:00  長度 01:00\n"
        "# 時間皆為錄音內相對時間\n"
        "00:08.0  人  狗與露：哈哈哈哈哈哈哈哈哈\n"
        "00:20.0  馬文(DJ)  馬文：今天天氣不錯\n"
    )
    assert out == expected


def _sample_candidates(approved=(False,)):
    clips = []
    for i, is_approved in enumerate(approved, 1):
        clips.append({
            "id": f"c{i:02d}",
            "approved": is_approved,
            "title": "馬文吐槽宵夜選擇",
            "post_type": "名場面",
            "from": 123.0,
            "to": 158.0,
            "events": [
                {"kind": "human", "speaker": "狗與露", "from": 124.0, "to": 127.5, "text": "修正後的字幕文字"},
                {"kind": "marvin", "voice": None, "from": 128.0, "text": "馬文台詞"},
                {"kind": "ack", "file": "assets/acks/music/music_ack_03.mp3", "from": 131.0, "text": "這首好聽"},
                {"kind": "song_card", "from": 135.0, "to": 139.0, "title": "七里香", "artist": "周杰倫"},
            ],
        })
    return {
        "recording": "/abs/path/2026-09-28 22-10-05.mkv",
        "rec_start": "2026-09-28 22:10:05",
        "marvin_audio": "resynth",
        "marvin_delay": 0.3,
        "clips": clips,
    }


def test_render_review_md_not_approved():
    candidates = _sample_candidates(approved=(False,))
    out = render_review_md(candidates)
    expected = (
        "# 素材候選審閱（2026-09-28 22:10:05，共 1 段）\n"
        "審閱方式：看完回覆要用的編號；想改字幕直接寫在回覆裡。\n"
        "\n"
        "## c01｜馬文吐槽宵夜選擇（名場面，35 秒，02:03–02:38）\n"
        "- 02:04 🧑狗與露：修正後的字幕文字\n"
        "- 02:08 🤖馬文：馬文台詞\n"
        "- 02:11 🤖馬文（罐頭）：這首好聽\n"
        "- 02:15 🎵正在播放：《七里香》— 周杰倫\n"
    )
    assert out == expected


def test_render_review_md_approved_marker_and_note():
    candidates = _sample_candidates(approved=(True,))
    candidates["clips"][0]["note"] = "備註文字"
    out = render_review_md(candidates)
    lines = out.splitlines()
    assert "## c01｜馬文吐槽宵夜選擇（名場面，35 秒，02:03–02:38） ✅" in lines
    assert "備註文字" in lines


def _valid_candidates():
    return {
        "marvin_audio": "resynth",
        "marvin_delay": 0.3,
        "clips": [
            {
                "id": "c01",
                "from": 0.0,
                "to": 10.0,
                "events": [
                    {"kind": "human", "speaker": "A", "from": 1.0, "to": 2.0, "text": "hi"},
                ],
            }
        ],
    }


def test_validate_candidates_ok(tmp_path):
    assert validate_candidates(_valid_candidates(), tmp_path) == []


def test_validate_candidates_duplicate_id(tmp_path):
    candidates = _valid_candidates()
    candidates["clips"].append(dict(candidates["clips"][0]))
    errors = validate_candidates(candidates, tmp_path)
    assert any("c01" in e and "重複" in e for e in errors)


def test_validate_candidates_from_gte_to(tmp_path):
    candidates = _valid_candidates()
    candidates["clips"][0]["from"] = 5.0
    candidates["clips"][0]["to"] = 5.0
    errors = validate_candidates(candidates, tmp_path)
    assert any("c01" in e for e in errors)


def test_validate_candidates_too_long(tmp_path):
    candidates = _valid_candidates()
    candidates["clips"][0]["to"] = 91.0
    candidates["clips"][0]["events"][0]["from"] = 1.0
    candidates["clips"][0]["events"][0]["to"] = 2.0
    errors = validate_candidates(candidates, tmp_path)
    assert any("c01" in e and "90" in e for e in errors)


def test_validate_candidates_event_out_of_range(tmp_path):
    candidates = _valid_candidates()
    candidates["clips"][0]["events"][0]["from"] = 20.0
    errors = validate_candidates(candidates, tmp_path)
    assert any("c01" in e and "超出範圍" in e for e in errors)


def test_validate_candidates_missing_ack_file(tmp_path):
    candidates = _valid_candidates()
    candidates["clips"][0]["events"] = [
        {"kind": "ack", "file": "assets/acks/does_not_exist.mp3", "from": 1.0, "text": "x"}
    ]
    errors = validate_candidates(candidates, tmp_path)
    assert any("c01" in e and "不存在" in e for e in errors)


def test_validate_candidates_unknown_kind(tmp_path):
    candidates = _valid_candidates()
    candidates["clips"][0]["events"] = [{"kind": "unknown", "from": 1.0}]
    errors = validate_candidates(candidates, tmp_path)
    assert any("c01" in e and "不明" in e for e in errors)


def _write_sine_wav(path, seconds=3, rate=48000, freq=440.0):
    import math

    n = int(seconds * rate)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            val = int(3000 * math.sin(2 * math.pi * freq * i / rate))
            frames += struct.pack("<h", val)
        wf.writeframes(bytes(frames))


@pytest.mark.slow
def test_timeline_end_to_end(tmp_path):
    wav = tmp_path / "2026-09-26 22-10-05.wav"
    _write_sine_wav(wav, seconds=3)

    rec_start = datetime(2026, 9, 26, 22, 10, 5).timestamp()

    db_path = tmp_path / "marvin.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE transcripts (speaker TEXT, guild_id INTEGER, channel_id INTEGER, "
        "text TEXT, timestamp REAL)"
    )
    conn.execute(
        "INSERT INTO transcripts (speaker, guild_id, channel_id, text, timestamp) VALUES (?, ?, ?, ?, ?)",
        ("狗與露", 1, 1, "哈囉", rec_start + 1.0),
    )
    conn.commit()
    conn.close()

    speech_log = tmp_path / "marvin_speech.log"
    speech_log.write_text(
        json.dumps({"start": rec_start + 0.5, "layer": 1, "voice": None, "src": "tts", "text": "嗨"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    bot_log = tmp_path / "bot_main.log"
    bot_ts = datetime.fromtimestamp(rec_start + 1.5).strftime("%Y-%m-%d %H:%M:%S,000")
    bot_log.write_text(
        f"{bot_ts} [INFO] cogs.voice_controller: 🗣️ [Ack:music] 播放 music_ack_03.mp3\n",
        encoding="utf-8",
    )

    out_txt = tmp_path / "out.timeline.txt"
    rc = main([
        "timeline", str(wav),
        "--db", str(db_path),
        "--speech-log-dir", str(tmp_path),
        "--bot-log-dir", str(tmp_path),
        "-o", str(out_txt),
    ])
    assert rc == 0

    out_json = out_txt.with_suffix(".json")
    assert out_txt.exists()
    assert out_json.exists()

    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert len(data["events"]) == 3
    kinds = {e["kind"] for e in data["events"]}
    assert kinds == {"human", "marvin", "ack"}


def test_render_review_md_song_card_without_artist():
    candidates = _sample_candidates(approved=(False,))
    candidates["clips"][0]["events"] = [{"kind": "song_card", "from": 135.0, "to": 139.0, "title": "七里香"}]
    assert "- 02:15 🎵正在播放：《七里香》" in render_review_md(candidates).splitlines()


def test_collect_events_sorted_by_time_across_sources():
    # 來源順序（人→馬文→舊 ack）跟時間順序相反，輸出必須依 from 排序
    human_rows = [("狗與露", "哈哈哈哈哈哈哈哈哈", 1030.0)]            # from 28.0
    speech_rows = [{"start": 1015.0, "voice": None, "src": "tts", "text": "嗨"}]  # from 15.0
    bot_acks = [{"start": 1005.0, "file": "music_ack_03.mp3"}]            # from 5.0
    events = collect_events(human_rows, speech_rows, bot_acks, rec_start=1000.0, duration=60.0)
    assert [(e["kind"], e["from"]) for e in events] == [("ack", 5.0), ("marvin", 15.0), ("human", 28.0)]


# ── OBS log 毫秒開始時間（檔名只到整秒，實測 21-36-08.mkv 真正開始是 21:36:08.505）──

def _write_obs_log(logs_dir, log_name, lines):
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / log_name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_find_obs_recording_start_ms(tmp_path):
    from scripts.clip_timeline import find_obs_recording_start

    rec = tmp_path / "2026-09-28 21-36-08.mkv"
    logs = tmp_path / "obs_logs"
    _write_obs_log(logs, "2026-09-28 21-22-06.txt", [
        "21:22:06.100: OBS 32.2.2 (mac)",
        "21:22:13.165: ==== Recording Start ===============================================",
        "21:22:13.165: [ffmpeg muxer: 'adv_file_output'] Writing file '/other/2026-09-28 21-22-13.mkv'...",
        "21:36:08.505: ==== Recording Start ===============================================",
        f"21:36:08.505: [ffmpeg muxer: 'adv_file_output'] Writing file '{rec}'...",
    ])
    assert find_obs_recording_start(rec, logs) == datetime(2026, 9, 28, 21, 36, 8, 505000)


def test_find_obs_recording_start_crosses_midnight(tmp_path):
    from scripts.clip_timeline import find_obs_recording_start

    rec = tmp_path / "2026-09-29 00-10-02.mkv"
    logs = tmp_path / "obs_logs"
    _write_obs_log(logs, "2026-09-28 23-50-00.txt", [
        "23:50:00.000: OBS 32.2.2 (mac)",
        f"00:10:02.250: [ffmpeg muxer: 'adv_file_output'] Writing file '{rec}'...",
    ])
    assert find_obs_recording_start(rec, logs) == datetime(2026, 9, 29, 0, 10, 2, 250000)


def test_find_obs_recording_start_searches_older_logs(tmp_path):
    from scripts.clip_timeline import find_obs_recording_start

    rec = tmp_path / "2026-09-28 21-36-08.mkv"
    logs = tmp_path / "obs_logs"
    _write_obs_log(logs, "2026-09-28 21-22-06.txt", [
        f"21:36:08.505: [ffmpeg muxer: 'adv_file_output'] Writing file '{rec}'...",
    ])
    _write_obs_log(logs, "2026-09-28 22-00-00.txt", ["22:00:00.000: OBS 32.2.2 (mac)"])
    assert find_obs_recording_start(rec, logs) == datetime(2026, 9, 28, 21, 36, 8, 505000)


def test_find_obs_recording_start_no_match_returns_none(tmp_path):
    from scripts.clip_timeline import find_obs_recording_start

    rec = tmp_path / "2026-09-28 21-36-08.mkv"
    logs = tmp_path / "obs_logs"
    _write_obs_log(logs, "2026-09-28 21-22-06.txt", [
        "21:22:13.165: [ffmpeg muxer: 'adv_file_output'] Writing file '/other/2026-09-28 21-22-13.mkv'...",
    ])
    assert find_obs_recording_start(rec, logs) is None
    assert find_obs_recording_start(rec, tmp_path / "does_not_exist") is None


def _timeline_fixture(tmp_path, *, speech_rows):
    wav = tmp_path / "2026-09-26 22-10-05.wav"
    _write_sine_wav(wav, seconds=5)
    db_path = tmp_path / "marvin.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE transcripts (speaker TEXT, guild_id INTEGER, channel_id INTEGER, "
        "text TEXT, timestamp REAL)"
    )
    conn.commit()
    conn.close()
    (tmp_path / "marvin_speech.log").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in speech_rows), encoding="utf-8"
    )
    (tmp_path / "bot_main.log").write_text("", encoding="utf-8")
    return wav, db_path


def _run_timeline(tmp_path, wav, db_path, obs_logs):
    out_txt = tmp_path / "out.timeline.txt"
    rc = main([
        "timeline", str(wav),
        "--db", str(db_path),
        "--speech-log-dir", str(tmp_path),
        "--bot-log-dir", str(tmp_path),
        "--obs-log-dir", str(obs_logs),
        "-o", str(out_txt),
    ])
    assert rc == 0
    return json.loads(out_txt.with_suffix(".json").read_text(encoding="utf-8"))


def test_timeline_uses_obs_log_start(tmp_path):
    obs_start = datetime(2026, 9, 26, 22, 10, 5, 500000).timestamp()
    wav, db_path = _timeline_fixture(tmp_path, speech_rows=[
        {"start": obs_start + 1.0, "layer": 1, "voice": None, "src": "tts", "text": "嗨"},
    ])
    obs_logs = tmp_path / "obs_logs"
    _write_obs_log(obs_logs, "2026-09-26 22-00-00.txt", [
        f"22:10:05.500: [ffmpeg muxer: 'adv_file_output'] Writing file '{wav}'...",
    ])
    data = _run_timeline(tmp_path, wav, db_path, obs_logs)
    assert data["rec_start_source"] == "obs_log"
    [ev] = [e for e in data["events"] if e["kind"] == "marvin"]
    assert ev["from"] == pytest.approx(1.0, abs=1e-3)


def test_timeline_falls_back_to_filename_start(tmp_path):
    fname_start = datetime(2026, 9, 26, 22, 10, 5).timestamp()
    wav, db_path = _timeline_fixture(tmp_path, speech_rows=[
        {"start": fname_start + 1.0, "layer": 1, "voice": None, "src": "tts", "text": "嗨"},
    ])
    data = _run_timeline(tmp_path, wav, db_path, tmp_path / "no_obs_logs")
    assert data["rec_start_source"] == "filename"
    [ev] = [e for e in data["events"] if e["kind"] == "marvin"]
    assert ev["from"] == pytest.approx(1.0, abs=1e-3)


# ── 台詞來源：satellite / local 程序也寫同一份 marvin_speech.log，不能混進 Discord 錄音的時間軸 ──

def test_collect_events_skips_non_discord_origin():
    speech_rows = [
        {"start": 1010.0, "src": "tts", "voice": None, "text": "舊紀錄沒有 origin"},
        {"start": 1011.0, "src": "tts", "voice": None, "text": "discord 講的", "origin": "discord"},
        {"start": 1012.0, "src": "tts", "voice": None, "text": "satellite 的新聞", "origin": "satellite"},
        {"start": 1013.0, "src": "song", "text": "satellite 的歌", "origin": "satellite"},
        {"start": 1014.0, "src": "ack", "text": "好選擇", "file": "a.mp3", "origin": "local"},
    ]
    events = collect_events([], speech_rows, [], rec_start=1000.0, duration=60.0)
    assert [e["text"] for e in events] == ["舊紀錄沒有 origin", "discord 講的"]


def test_collect_events_dedupes_identical_human_rows():
    human_rows = [
        ("狗與露", "馬文播放陳綺貞的旅行的意義", 1020.0),
        ("狗與露", "馬文播放陳綺貞的旅行的意義", 1020.0),
        ("狗與露", "馬文播放陳綺貞的旅行的意義", 1050.0),  # 不同時間 = 真的講了兩次，保留
    ]
    events = collect_events(human_rows, [], [], rec_start=1000.0, duration=60.0)
    assert len([e for e in events if e["kind"] == "human"]) == 2

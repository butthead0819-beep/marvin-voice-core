import json
import sqlite3
import struct
import subprocess
import wave
from datetime import datetime
from pathlib import Path

import pytest
from PIL import ImageFont

from scripts.make_subtitle_video import (
    Cue,
    assign_colors,
    build_cues,
    build_ffmpeg_cmd,
    format_srt,
    human_cue_window,
    main,
    marvin_cue_window,
    parse_obs_start,
    parse_srt,
    read_speech_log,
    render_frame,
    segment_timeline,
    wrap_text,
)

STHEITI = "/System/Library/Fonts/STHeiti Medium.ttc"


def test_parse_obs_start():
    assert parse_obs_start("2026-09-26 22-10-05.mkv") == datetime(2026, 9, 26, 22, 10, 5)
    assert parse_obs_start("clip.wav") is None
    assert parse_obs_start("/a/b/OBS 2026-09-26 22-10-05 final.mp4") == datetime(2026, 9, 26, 22, 10, 5)


def test_human_cue_window():
    assert human_cue_window(100.0, "一" * 9) == (98.0, 100.8)
    assert human_cue_window(100.0, "一") == (99.2, 100.8)
    assert human_cue_window(100.0, "一" * 100) == (92.0, 100.8)


def test_marvin_cue_window():
    assert marvin_cue_window(50.0, "一" * 8) == (50.0, 52.0)
    assert marvin_cue_window(50.0, "一") == (50.0, 51.0)


def test_build_cues_basic_anonymize():
    human_rows = [
        ("狗與露", "哈哈哈哈哈哈哈哈哈", 1010.0),
        ("showay", "你好", 1020.0),
        ("狗與露", "再見", 1030.0),
        ("someone", "掉出範圍", 900.0),
    ]
    marvin_rows = [
        {"start": 1015.0, "voice": None, "text": "我腦袋有行星那麼大"},
        {"start": 1016.0, "voice": "en-US-GuyNeural", "text": "yo"},
    ]
    cues = build_cues(
        human_rows,
        marvin_rows,
        rec_start=1000.0,
        duration=60.0,
        offset=0.0,
        anonymize=True,
        aliases={},
        include_marvin=True,
    )
    labels_in_order = [c.label for c in cues]
    assert labels_in_order == ["成員A", "馬文", "Marmo", "成員B", "成員A"]

    # 手算：成員A 9字 dur=2.0 → (8.0,10.8)；馬文 9字 dur=2.25 → (15.0,17.25)；Marmo 2字 dur=1.0 → (16.0,17.0)
    #       成員B 2字 dur=0.8 → (19.2,20.8)；成員A 2字 → (29.2,30.8)
    expected = [(8.0, 10.8), (15.0, 17.25), (16.0, 17.0), (19.2, 20.8), (29.2, 30.8)]
    for c, (s, e) in zip(cues, expected):
        assert c.start == pytest.approx(s)
        assert c.end == pytest.approx(e)


def test_build_cues_offset():
    human_rows = [("狗與露", "哈哈哈哈哈哈哈哈哈", 1010.0)]
    cues = build_cues(
        human_rows, [], rec_start=1000.0, duration=60.0, offset=1.5,
        anonymize=True, aliases={}, include_marvin=True,
    )
    assert cues[0].start == pytest.approx(9.5)
    assert cues[0].end == pytest.approx(12.3)


def test_build_cues_no_marvin():
    human_rows = [("狗與露", "你好", 1010.0)]
    marvin_rows = [{"start": 1015.0, "voice": None, "text": "嗨"}]
    cues = build_cues(
        human_rows, marvin_rows, rec_start=1000.0, duration=60.0, offset=0.0,
        anonymize=True, aliases={}, include_marvin=False,
    )
    assert all(c.label not in ("馬文", "Marmo") for c in cues)
    assert len(cues) == 1


def test_build_cues_aliases():
    human_rows = [
        ("狗與露", "哈哈哈哈哈哈哈哈哈", 1010.0),
        ("showay", "你好", 1020.0),
    ]
    cues = build_cues(
        human_rows, [], rec_start=1000.0, duration=60.0, offset=0.0,
        anonymize=True, aliases={"showay": "小秀"}, include_marvin=True,
    )
    labels = [c.label for c in cues]
    assert labels == ["成員A", "小秀"]


def test_build_cues_no_anonymize_uses_real_names():
    human_rows = [
        ("狗與露", "哈哈哈哈哈哈哈哈哈", 1010.0),
        ("showay", "你好", 1020.0),
    ]
    cues = build_cues(
        human_rows, [], rec_start=1000.0, duration=60.0, offset=0.0,
        anonymize=False, aliases={}, include_marvin=True,
    )
    labels = [c.label for c in cues]
    assert labels == ["狗與露", "showay"]


def test_build_cues_overlap_trim_same_label():
    # row1: 1 字 -> dur=0.8, window (9.2, 10.8)
    # row2: 9 字 -> dur=2.0, window (10.0, 12.8)
    # 排序後 row1(start 9.2) 在前，row1.end(10.8) > row2.start(10.0) -> 裁到 10.0
    human_rows = [
        ("狗與露", "好", 1010.0),
        ("狗與露", "九" * 9, 1012.0),
    ]
    cues = build_cues(
        human_rows, [], rec_start=1000.0, duration=60.0, offset=0.0,
        anonymize=False, aliases={}, include_marvin=True,
    )
    cues_sorted = sorted(cues, key=lambda c: c.start)
    assert cues_sorted[0].start == pytest.approx(9.2)
    assert cues_sorted[0].end == pytest.approx(10.0)
    assert cues_sorted[1].start == pytest.approx(10.0)
    assert cues_sorted[1].end == pytest.approx(12.8)


def test_build_cues_clamp():
    human_rows = [("狗與露", "你好", 1000.5)]
    cues = build_cues(
        human_rows, [], rec_start=1000.0, duration=1.0, offset=0.0,
        anonymize=False, aliases={}, include_marvin=True,
    )
    # 2字 dur=0.8，end=0.5 → 相對 (-0.3, 1.3) → clamp 成 (0.0, 1.0)
    assert len(cues) == 1
    assert cues[0].start == pytest.approx(0.0)
    assert cues[0].end == pytest.approx(1.0)


def test_format_srt_parse_srt_roundtrip():
    cues = [
        Cue(start=1.234, end=3.5, label="馬文", text="你好嗎"),
        Cue(start=4.0, end=6.789, label="成員A", text="還不錯\n第二行"),
    ]
    srt = format_srt(cues)
    parsed = parse_srt(srt)
    assert len(parsed) == 2
    assert parsed[0].label == "馬文"
    assert parsed[0].text == "你好嗎"
    assert parsed[0].start == pytest.approx(1.234, abs=0.001)
    assert parsed[0].end == pytest.approx(3.5, abs=0.001)
    assert parsed[1].label == "成員A"
    assert parsed[1].text == "還不錯\n第二行"
    assert parsed[1].end == pytest.approx(6.789, abs=0.001)


def test_parse_srt_tolerant_formats():
    content = (
        "00:00:01,000 --> 00:00:02,500\n"
        "沒有序號行也沒有冒號\n"
        "\n"
        "2\n"
        "00:00:03.000 --> 00:00:04.000\n"
        "馬文：句點毫秒版\n"
    )
    parsed = parse_srt(content)
    assert len(parsed) == 2
    assert parsed[0].label == ""
    assert parsed[0].text == "沒有序號行也沒有冒號"
    assert parsed[1].label == "馬文"
    assert parsed[1].text == "句點毫秒版"


def test_assign_colors():
    cues = [
        Cue(0, 1, "馬文", "a"),
        Cue(1, 2, "Marmo", "b"),
        Cue(2, 3, "", "c"),
        Cue(3, 4, "成員A", "d"),
        Cue(4, 5, "成員B", "e"),
    ]
    colors = assign_colors(cues)
    assert colors["馬文"] == "#4FC3F7"
    assert colors["Marmo"] == "#FFB74D"
    assert colors[""] == "#FFFFFF"
    assert colors["成員A"] == "#FFF176"
    assert colors["成員B"] == "#F48FB1"


def test_segment_timeline():
    a = Cue(1, 3, "A", "a")
    b = Cue(2, 4, "B", "b")
    segs = segment_timeline([a, b], 5.0)
    bounds = [(round(s, 3), round(e, 3), [c.label for c in active]) for s, e, active in segs]
    assert bounds == [
        (0.0, 1.0, []),
        (1.0, 2.0, ["A"]),
        (2.0, 3.0, ["A", "B"]),
        (3.0, 4.0, ["B"]),
        (4.0, 5.0, []),
    ]


def test_segment_timeline_limits_to_last_four():
    cues = [Cue(0, 10, f"L{i}", f"t{i}") for i in range(6)]
    segs = segment_timeline(cues, 10.0)
    for _, _, active in segs:
        assert len(active) <= 4
    _, _, active_mid = segs[0]
    assert [c.label for c in active_mid] == ["L2", "L3", "L4", "L5"]


def test_wrap_text():
    if not Path(STHEITI).exists():
        pytest.skip("字型檔不存在")
    font = ImageFont.truetype(STHEITI, 64)
    text = "測" * 40
    max_width = 920
    lines = wrap_text(text, font, max_width)
    assert "".join(lines) == text
    for line in lines:
        assert font.getlength(line) <= max_width


def test_wrap_text_preserves_newlines():
    if not Path(STHEITI).exists():
        pytest.skip("字型檔不存在")
    font = ImageFont.truetype(STHEITI, 64)
    text = "第一行\n第二行"
    lines = wrap_text(text, font, 920)
    assert lines == ["第一行", "第二行"]


def test_render_frame_empty_is_black():
    if not Path(STHEITI).exists():
        pytest.skip("字型檔不存在")
    font = ImageFont.truetype(STHEITI, 64)
    img = render_frame([], {}, font)
    assert max(ch[1] for ch in img.getextrema()) == 0


def test_render_frame_marvin_color_present():
    if not Path(STHEITI).exists():
        pytest.skip("字型檔不存在")
    font = ImageFont.truetype(STHEITI, 64)
    cue = Cue(0, 5, "馬文", "你好")
    colors = assign_colors([cue])
    img = render_frame([cue], colors, font)
    target = (0x4F, 0xC3, 0xF7)
    found = False
    for px in img.getdata():
        if all(abs(px[i] - target[i]) <= 30 for i in range(3)):
            found = True
            break
    assert found


def test_build_ffmpeg_cmd():
    cmd = build_ffmpeg_cmd("concat.txt", "rec.mkv", "out.mp4")
    assert cmd == [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "concat.txt",
        "-i", "rec.mkv",
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf", "fps=30,format=yuv420p",
        "-c:v", "libx264", "-preset", "medium", "-crf", "23",
        "-c:a", "aac", "-b:a", "160k",
        "-shortest", "-movflags", "+faststart", "out.mp4",
    ]


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
def test_render_end_to_end(tmp_path):
    wav = tmp_path / "rec.wav"
    _write_sine_wav(wav, seconds=3)

    srt_content = (
        "1\n00:00:00,500 --> 00:00:01,500\n馬文：你好\n\n"
        "2\n00:00:01,800 --> 00:00:02,600\n成員A：哈囉\n"
    )
    srt_path = tmp_path / "subs.srt"
    srt_path.write_text(srt_content, encoding="utf-8")

    out = tmp_path / "out.mp4"
    rc = main(["render", str(wav), str(srt_path), "-o", str(out)])
    assert rc == 0
    assert out.exists()

    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,codec_name",
            "-of", "csv=p=0", str(out),
        ],
        capture_output=True, text=True, check=True,
    )
    # ffprobe's csv output order is fixed (codec_name,width,height) regardless
    # of the order fields are requested in.
    codec, w, h = probe.stdout.strip().split(",")
    assert int(w) == 1080
    assert int(h) == 1920
    assert codec == "h264"

    aprobe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(out),
        ],
        capture_output=True, text=True, check=True,
    )
    assert aprobe.stdout.strip() != ""

    dprobe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "csv=p=0", str(out),
        ],
        capture_output=True, text=True, check=True,
    )
    duration = float(dprobe.stdout.strip())
    assert 2.8 <= duration <= 3.3

    frame_mid = tmp_path / "mid.png"
    subprocess.run(
        ["ffmpeg", "-y", "-ss", "1.0", "-i", str(out), "-frames:v", "1", str(frame_mid)],
        capture_output=True, check=True,
    )
    from PIL import Image

    img = Image.open(frame_mid).convert("RGB")
    target = (0x4F, 0xC3, 0xF7)
    found = any(
        all(abs(px[i] - target[i]) <= 30 for i in range(3)) for px in img.getdata()
    )
    assert found

    frame_early = tmp_path / "early.png"
    subprocess.run(
        ["ffmpeg", "-y", "-ss", "0.2", "-i", str(out), "-frames:v", "1", str(frame_early)],
        capture_output=True, check=True,
    )
    img2 = Image.open(frame_early).convert("RGB")
    maxval = max(max(px) for px in img2.getdata())
    assert maxval <= 16


@pytest.mark.slow
def test_prepare_end_to_end(tmp_path):
    db_path = tmp_path / "marvin.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE transcripts (speaker TEXT, guild_id TEXT, channel_id TEXT, text TEXT, timestamp REAL)"
    )
    rec_start_dt = datetime(2026, 9, 26, 22, 10, 5)
    rec_start = rec_start_dt.timestamp()
    conn.execute(
        "INSERT INTO transcripts VALUES (?,?,?,?,?)",
        ("狗與露", "g", "c", "哈囉大家好", rec_start + 1.0),
    )
    conn.execute(
        "INSERT INTO transcripts VALUES (?,?,?,?,?)",
        ("showay", "g", "c", "早安", rec_start + 2.0),
    )
    conn.commit()
    conn.close()

    speech_log = tmp_path / "marvin_speech.log"
    speech_log.write_text(
        json.dumps({"start": rec_start + 1.5, "layer": 0, "voice": None, "src": "tts", "text": "我在聽"})
        + "\n",
        encoding="utf-8",
    )

    wav = tmp_path / "2026-09-26 22-10-05.wav"
    _write_sine_wav(wav, seconds=3)

    srt_path = tmp_path / "out.srt"
    rc = main([
        "prepare", str(wav),
        "--db", str(db_path),
        "--speech-log-dir", str(tmp_path),
        "-o", str(srt_path),
    ])
    assert rc == 0
    cues = parse_srt(srt_path.read_text(encoding="utf-8"))
    assert len(cues) == 3
    got = [(c.label, c.text, round(c.start, 3), round(c.end, 3)) for c in sorted(cues, key=lambda c: c.start)]
    # 手算：狗與露 5字 dur=1.111、end=1.0 → (0.0 clamp, 1.8)；showay 2字 end=2.0 → (1.2, 2.8)；馬文 3字 dur=1.0 → (1.5, 2.5)
    assert got == [
        ("成員A", "哈囉大家好", 0.0, 1.8),
        ("成員B", "早安", 1.2, 2.8),
        ("馬文", "我在聽", 1.5, 2.5),
    ]


def test_prepare_no_start_no_filename_errors(tmp_path):
    wav = tmp_path / "recording.wav"
    _write_sine_wav(wav, seconds=1)
    rc = main(["prepare", str(wav), "-o", str(tmp_path / "out.srt")])
    assert rc == 2


def test_segment_timeline_merges_adjacent_identical_active_sets():
    # L0 在 5 秒結束，但超過 4 則只留最後 4 則 → [0,5) 與 [5,10) 的 active 都是 L1..L4 → 合併成一段
    cues = [Cue(0, 5, "L0", "t0")] + [Cue(0, 10, f"L{i}", f"t{i}") for i in range(1, 5)]
    segs = segment_timeline(cues, 10.0)
    assert [(round(a, 3), round(b, 3), [c.label for c in act]) for a, b, act in segs] == [
        (0.0, 10.0, ["L1", "L2", "L3", "L4"]),
    ]

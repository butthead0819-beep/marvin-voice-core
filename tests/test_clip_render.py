import json
import math
import struct
import subprocess
import wave
from pathlib import Path

import pytest
from PIL import Image, ImageFont

from audio_mixing import TTS_LOUDNESS_AF
from scripts.clip_render import (
    Overlay,
    assign_speakers,
    build_display_items,
    build_mix_cmd,
    build_overlays,
    fetch_avatar,
    load_marvin_avatar,
    make_generic_avatar,
    render_candidates,
    render_frame,
    render_index_md,
    segment_items,
    split_for_reading,
)

STHEITI = "/System/Library/Fonts/STHeiti Medium.ttc"


# ---------------------------------------------------------------------------
# split_for_reading
# ---------------------------------------------------------------------------

def test_split_for_reading_short_untouched():
    assert split_for_reading("一二三", 24) == ["一二三"]


def test_split_for_reading_end_punct():
    assert split_for_reading("一二三。四五六，七八九", 4) == ["一二三。", "四五六，", "七八九"]


def test_split_for_reading_hard_split():
    text = "一" * 30
    result = split_for_reading(text, 24)
    assert result == ["一" * 24, "一" * 6]


def test_split_for_reading_reassembles():
    for text in ["一二三。四五六，七八九", "一" * 30, "沒有標點的長句子沒有標點的長句子沒有標點的長句子"]:
        assert "".join(split_for_reading(text, 24)) == text


# ---------------------------------------------------------------------------
# build_overlays
# ---------------------------------------------------------------------------

def _write_sine_wav(path, seconds=1, rate=48000, freq=440.0):
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


def _write_sine_mp3(path, seconds=1):
    wav_path = path.with_suffix(".src.wav")
    _write_sine_wav(wav_path, seconds=seconds)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(wav_path), str(path)],
        check=True, capture_output=True,
    )


def _base_clip():
    return {
        "id": "c01",
        "approved": True,
        "title": "測試片段",
        "post_type": "名場面",
        "from": 100.0,
        "to": 120.0,
        "events": [
            {"kind": "human", "speaker": "狗與露", "from": 101.0, "to": 103.0, "text": "哈囉"},
            {"kind": "marvin", "voice": None, "from": 104.0, "text": "我在聽"},
            {"kind": "ack", "file": "assets/acks/music/music_ack_03.mp3", "from": 106.0, "text": "這首好聽"},
            {"kind": "song_card", "from": 108.0, "to": 112.0, "title": "七里香", "artist": "周杰倫"},
        ],
    }


def test_build_overlays_resynth(tmp_path):
    clip = _base_clip()
    candidates = {"marvin_audio": "resynth", "marvin_delay": 0.3}

    ack_path = tmp_path / "assets" / "acks" / "music" / "music_ack_03.mp3"
    ack_path.parent.mkdir(parents=True)
    ack_path.write_bytes(b"fake-ack")

    sfx_path = tmp_path / "assets" / "dj_sfx" / "scratch.wav"
    sfx_path.parent.mkdir(parents=True)
    sfx_path.write_bytes(b"fake-sfx")

    def synth_fn(text, voice):
        return "/tmp/fake_marvin.mp3"

    overlays = build_overlays(clip, candidates, synth_fn=synth_fn, repo_root=tmp_path)

    assert len(overlays) == 3
    assert overlays[0] == Overlay(path="/tmp/fake_marvin.mp3", delay_ms=4300, af=TTS_LOUDNESS_AF)
    assert overlays[1] == Overlay(path=str(ack_path), delay_ms=6300, af=TTS_LOUDNESS_AF)
    assert overlays[2] == Overlay(path=str(sfx_path), delay_ms=8000, af="volume=0.6")


def test_build_overlays_original_mode(tmp_path):
    clip = _base_clip()
    candidates = {"marvin_audio": "original", "marvin_delay": 0.3}

    sfx_path = tmp_path / "assets" / "dj_sfx" / "scratch.wav"
    sfx_path.parent.mkdir(parents=True)
    sfx_path.write_bytes(b"fake-sfx")

    overlays = build_overlays(clip, candidates, synth_fn=lambda t, v: "/x.mp3", repo_root=tmp_path)

    assert len(overlays) == 1
    assert overlays[0].af == "volume=0.6"
    assert overlays[0].delay_ms == 8000


def test_build_overlays_missing_sfx_warns(tmp_path, capsys):
    clip = _base_clip()
    candidates = {"marvin_audio": "original", "marvin_delay": 0.3}

    overlays = build_overlays(clip, candidates, synth_fn=lambda t, v: "/x.mp3", repo_root=tmp_path)

    assert overlays == []
    assert "scratch.wav" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# build_mix_cmd
# ---------------------------------------------------------------------------

def test_build_mix_cmd_two_overlays():
    overlays = [
        Overlay(path="a.mp3", delay_ms=4300, af=TTS_LOUDNESS_AF),
        Overlay(path="b.mp3", delay_ms=8000, af="volume=0.6"),
    ]
    cmd = build_mix_cmd("base.wav", overlays, "out.wav", 20.0)
    expected_fc = (
        f"[1:a]aformat=sample_rates=48000:channel_layouts=stereo,{TTS_LOUDNESS_AF},adelay=4300|4300[o1];"
        "[2:a]aformat=sample_rates=48000:channel_layouts=stereo,volume=0.6,adelay=8000|8000[o2];"
        "[0:a][o1][o2]amix=inputs=3:normalize=0:duration=first,loudnorm=I=-14:TP=-1.5:LRA=11[out]"
    )
    assert cmd == [
        "ffmpeg", "-y", "-i", "base.wav",
        "-i", "a.mp3",
        "-i", "b.mp3",
        "-filter_complex", expected_fc,
        "-map", "[out]",
        "-t", "20.000",
        "-ar", "48000", "-ac", "2",
        "out.wav",
    ]


def test_build_mix_cmd_no_overlays():
    cmd = build_mix_cmd("base.wav", [], "out.wav", 20.0)
    expected_fc = "[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[out]"
    assert cmd == [
        "ffmpeg", "-y", "-i", "base.wav",
        "-filter_complex", expected_fc,
        "-map", "[out]",
        "-t", "20.000",
        "-ar", "48000", "-ac", "2",
        "out.wav",
    ]


# ---------------------------------------------------------------------------
# build_display_items
# ---------------------------------------------------------------------------

def test_build_display_items_basic():
    clip = {
        "from": 100.0,
        "to": 110.0,
        "events": [
            {"kind": "human", "speaker": "狗與露", "from": 101.0, "to": 103.0, "text": "哈囉"},
            {"kind": "marvin", "voice": None, "from": 104.0, "text": "我在聽"},
        ],
    }

    items = build_display_items(
        clip, delay=0.3, marvin_audio="resynth", audio_len_fn=lambda p: 2.0,
    )

    human = [it for it in items if it.kind == "human"][0]
    marvin = [it for it in items if it.kind == "marvin"][0]

    assert (human.start, human.end) == (1.0, 3.0)
    assert (round(marvin.start, 3), round(marvin.end, 3)) == (4.3, 6.6)


def test_build_display_items_clamped_to_duration():
    clip = {
        "from": 0.0,
        "to": 10.0,
        "events": [
            {"kind": "human", "speaker": "A", "from": 8.0, "to": 15.0, "text": "超出範圍"},
        ],
    }
    items = build_display_items(clip, delay=0.0, marvin_audio="resynth", audio_len_fn=lambda p: 2.0)
    assert len(items) == 1
    assert items[0].start == 8.0
    assert items[0].end == 10.0


def test_build_display_items_long_text_split_contiguous():
    long_text = "一二三四五六七八九十" * 3  # 30 chars
    clip = {
        "from": 0.0,
        "to": 20.0,
        "events": [
            {"kind": "human", "speaker": "A", "from": 1.0, "to": 11.0, "text": long_text},
        ],
    }
    items = build_display_items(clip, delay=0.0, marvin_audio="resynth", audio_len_fn=lambda p: 2.0)
    assert len(items) > 1
    assert items[0].start == 1.0
    assert items[-1].end == 11.0
    for a, b in zip(items, items[1:]):
        assert a.end == b.start
    assert "".join(it.text for it in items) == long_text


# ---------------------------------------------------------------------------
# assign_speakers
# ---------------------------------------------------------------------------

def test_assign_speakers_order_and_fixed_colors():
    from scripts.clip_render import DisplayItem

    items = [
        DisplayItem(kind="human", start=0, end=1, speaker="狗與露", text="a"),
        DisplayItem(kind="human", start=1, end=2, speaker="showay", text="b"),
        DisplayItem(kind="marvin", start=2, end=3, speaker=None, text="c"),
    ]
    speakers = assign_speakers(items)

    assert speakers["狗與露"]["color"] == "#FFF176"
    assert speakers["狗與露"]["shape"] == "dot"
    assert speakers["showay"]["color"] == "#F48FB1"
    assert speakers["showay"]["shape"] == "triangle"
    assert speakers["馬文"]["color"] == "#4FC3F7"


# ---------------------------------------------------------------------------
# make_generic_avatar / load_marvin_avatar
# ---------------------------------------------------------------------------

def _close(px, target, tol=30):
    return all(abs(px[i] - target[i]) <= tol for i in range(3))


def test_make_generic_avatar_shape_and_bg():
    img = make_generic_avatar("#FFF176", "triangle", size=96)
    assert img.size == (96, 96)

    center = img.getpixel((48, 48))
    assert _close(center[:3], (0xFF, 0xFF, 0xFF))

    target_bg = (0xFF, 0xF1, 0x76)
    found_bg = any(
        _close(img.getpixel((x, 48))[:3], target_bg) for x in range(0, 20)
    )
    assert found_bg

    corner = img.getpixel((1, 1))
    assert corner[3] == 0 or corner[:3] == (0, 0, 0)


def test_load_marvin_avatar_from_file(tmp_path):
    src = tmp_path / "red.png"
    Image.new("RGB", (300, 200), (255, 0, 0)).save(src)

    img = load_marvin_avatar(src, size=96)
    assert img.size == (96, 96)
    center = img.getpixel((48, 48))
    assert _close(center[:3], (255, 0, 0))


def test_load_marvin_avatar_missing_falls_back(tmp_path):
    img = load_marvin_avatar(tmp_path / "nope.png", size=96)
    assert img.size == (96, 96)
    target = (0x4F, 0xC3, 0xF7)
    found = any(_close(img.getpixel((x, 48))[:3], target) for x in range(0, 20))
    assert found


# ---------------------------------------------------------------------------
# render_frame
# ---------------------------------------------------------------------------

def test_render_frame_human_and_song_card():
    from scripts.clip_render import DisplayItem

    items = [
        DisplayItem(kind="human", start=0, end=1, speaker="狗與露", text="哈囉"),
        DisplayItem(kind="song_card", start=1, end=2, title="七里香", artist="周杰倫"),
    ]
    speakers = assign_speakers(items)
    font = ImageFont.truetype(STHEITI, 40)

    img = render_frame(items, speakers, font)
    pixels = list(img.getdata())

    target_speaker = (0xFF, 0xF1, 0x76)
    assert any(_close(p, target_speaker) for p in pixels)

    target_card_bg = (0x1E, 0x1E, 0x1E)
    assert any(_close(p, target_card_bg, tol=5) for p in pixels)


def test_render_frame_empty_is_black():
    font = ImageFont.truetype(STHEITI, 40)
    img = render_frame([], {}, font)
    assert max(max(px) for px in img.getdata()) == 0


# ---------------------------------------------------------------------------
# render_index_md
# ---------------------------------------------------------------------------

def test_render_index_md_no_real_names():
    entries = [
        {
            "id": "c01",
            "title": "馬文吐槽宵夜選擇",
            "post_type": "名場面",
            "length_s": 35,
            "lines": ["成員A：吃泡麵好嗎", "成員B：不要", "馬文：我在聽", "🎵 正在播放：《七里香》— 周杰倫"],
        },
    ]
    md = render_index_md("2026-09-28 22:10:05", entries)

    assert "狗與露" not in md
    assert "showay" not in md
    assert "成員A" in md
    assert "成員B" in md
    assert "| c01 | 馬文吐槽宵夜選擇 | 名場面 | 35 秒 | c01.mp4 |" in md


# ---------------------------------------------------------------------------
# fetch_avatar
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_avatar(tmp_path):
    calls = []

    def fake_urlopen(req):
        calls.append(req)
        if len(calls) == 1:
            return _FakeResponse(json.dumps({"id": "1", "avatar": "abc"}).encode("utf-8"))
        return _FakeResponse(b"\x89PNG-fake-bytes")

    out_path = tmp_path / "avatar.png"
    fetch_avatar("secret-token", out_path, urlopen=fake_urlopen)

    assert out_path.read_bytes() == b"\x89PNG-fake-bytes"
    assert len(calls) == 2
    assert calls[1].full_url == "https://cdn.discordapp.com/avatars/1/abc.png?size=256"
    assert calls[0].headers.get("Authorization") == "Bot secret-token"
    assert calls[1].get_header("Authorization") is None  # CDN 不可帶 token
    assert "Authorization" not in calls[1].headers


# ---------------------------------------------------------------------------
# end-to-end
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_render_candidates_end_to_end(tmp_path):
    recording = tmp_path / "rec.wav"
    _write_sine_wav(recording, seconds=6)

    ack_dir = tmp_path / "assets" / "acks" / "music"
    ack_dir.mkdir(parents=True)
    ack_path = ack_dir / "music_ack_03.mp3"
    _write_sine_mp3(ack_path, seconds=1)

    sfx_dir = tmp_path / "assets" / "dj_sfx"
    sfx_dir.mkdir(parents=True)
    _write_sine_mp3(sfx_dir / "scratch.wav", seconds=1)

    fake_marvin_mp3 = tmp_path / "fake_marvin.mp3"
    _write_sine_mp3(fake_marvin_mp3, seconds=1)

    candidates = {
        "recording": str(recording),
        "rec_start": "2026-09-28 22:10:05",
        "marvin_audio": "resynth",
        "marvin_delay": 0.3,
        "clips": [
            {
                "id": "c01",
                "approved": True,
                "title": "測試片段",
                "post_type": "名場面",
                "from": 1.0,
                "to": 5.0,
                "events": [
                    {"kind": "human", "speaker": "狗與露", "from": 1.5, "to": 2.0, "text": "哈囉"},
                    {"kind": "marvin", "voice": None, "from": 2.5, "text": "我在聽"},
                    {"kind": "ack", "file": "assets/acks/music/music_ack_03.mp3", "from": 3.0, "text": "這首好聽"},
                    {"kind": "song_card", "from": 3.5, "to": 4.5, "title": "七里香", "artist": "周杰倫"},
                ],
            },
        ],
    }

    out_dir = tmp_path / "out"
    entries = render_candidates(
        candidates, out_dir,
        synth_fn=lambda text, voice: str(fake_marvin_mp3),
        repo_root=tmp_path,
        font_path=STHEITI,
    )
    assert len(entries) == 1

    out_mp4 = out_dir / "c01.mp4"
    assert out_mp4.exists()

    vprobe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,codec_name",
            "-of", "csv=p=0", str(out_mp4),
        ],
        capture_output=True, text=True, check=True,
    )
    codec, w, h = vprobe.stdout.strip().split(",")
    assert int(w) == 1080
    assert int(h) == 1920
    assert codec == "h264"

    aprobe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(out_mp4),
        ],
        capture_output=True, text=True, check=True,
    )
    assert aprobe.stdout.strip() != ""

    dprobe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out_mp4)],
        capture_output=True, text=True, check=True,
    )
    duration = float(dprobe.stdout.strip())
    assert 3.8 <= duration <= 4.3

    index_md = out_dir / "index.md"
    assert index_md.exists()
    content = index_md.read_text(encoding="utf-8")
    assert "狗與露" not in content
    assert "成員A" in content


def test_index_lines_chronological_and_anonymous():
    from scripts.clip_render import index_lines
    clip = {"events": [
        {"kind": "marvin", "voice": None, "from": 104.0, "text": "馬文回嘴"},
        {"kind": "human", "speaker": "狗與露", "from": 101.0, "to": 103.0, "text": "第一句"},
        {"kind": "song_card", "from": 108.0, "to": 110.0, "title": "七里香", "artist": "周杰倫"},
        {"kind": "human", "speaker": "showay", "from": 106.0, "to": 107.0, "text": "第二人"},
        {"kind": "human", "speaker": "狗與露", "from": 109.0, "to": 110.0, "text": "又是我"},
    ]}
    speakers = {"狗與露": {"index": 0}, "showay": {"index": 1}}
    assert index_lines(clip, speakers) == [
        "成員A：第一句",
        "馬文：馬文回嘴",
        "成員B：第二人",
        "🎵 正在播放：《七里香》— 周杰倫",
        "成員A：又是我",
    ]


def test_render_frame_song_card_text_inside_box():
    from scripts.clip_render import render_frame, DisplayItem
    font_path = "/System/Library/Fonts/STHeiti Medium.ttc"
    import os
    if not os.path.exists(font_path):
        pytest.skip("字型檔不存在")
    from PIL import ImageFont
    font = ImageFont.truetype(font_path, 60)
    item = DisplayItem(kind="song_card", start=0, end=1, title="七里香七里香", artist="周杰倫")
    img = render_frame([item], {}, font)
    w, h = img.size
    px = img.load()
    box_ys = [y for y in range(h) if px[540, y] == (0x1E, 0x1E, 0x1E) or px[90, y] == (0x1E, 0x1E, 0x1E)]
    text_ys = [y for y in range(h) for x in range(80, 1000, 4) if min(px[x, y]) > 200]
    assert box_ys and text_ys
    assert min(box_ys) <= min(text_ys) and max(text_ys) <= max(box_ys), "字超出卡片範圍"


def test_build_overlays_negative_delay_clamped_to_zero(tmp_path):
    clip = {"from": 100.0, "to": 110.0, "events": [
        {"kind": "marvin", "voice": None, "from": 99.5, "text": "早了"},  # 99.5+0.3-100 = -0.2 → 0
    ]}
    overlays = build_overlays(clip, {"marvin_audio": "resynth", "marvin_delay": 0.3},
                              synth_fn=lambda t, v: "/x.mp3", repo_root=tmp_path)
    assert overlays[0].delay_ms == 0


def test_segment_items_keeps_last_four():
    from scripts.clip_render import DisplayItem
    items = [DisplayItem(kind="human", start=0, end=10, speaker=f"s{i}", text=f"t{i}") for i in range(5)]
    segs = segment_items(items, 10.0)
    assert [it.speaker for it in segs[0][2]] == ["s1", "s2", "s3", "s4"]


def test_render_frame_text_uses_speaker_color_not_white():
    from scripts.clip_render import DisplayItem
    items = [DisplayItem(kind="human", start=0, end=1, speaker="狗與露", text="哈囉哈囉哈囉")]
    speakers = assign_speakers(items)
    img = render_frame(items, speakers, ImageFont.truetype(STHEITI, 60))
    px = img.load()
    text_region = [px[x, y] for x in range(200, 1000, 2) for y in range(0, img.size[1], 2)]
    assert any(_close(p, (0xFF, 0xF1, 0x76)) for p in text_region)
    assert not any(min(p) > 240 for p in text_region), "文字區不該有白字"


def test_load_marvin_avatar_is_circular(tmp_path):
    src = tmp_path / "red.png"
    Image.new("RGB", (300, 200), (255, 0, 0)).save(src)
    img = load_marvin_avatar(src, size=96)
    assert img.getpixel((1, 1))[3] == 0  # 角落透明＝有圓形遮罩


def test_render_candidates_only_approved_by_default(tmp_path, monkeypatch):
    import scripts.clip_render as cr
    rendered = []
    monkeypatch.setattr(cr, "_render_clip", lambda clip, *a, **k: rendered.append(clip["id"]) or
                        {"id": clip["id"], "title": "", "post_type": "", "length_s": 1, "lines": []})
    cands = {"rec_start": "x", "clips": [{"id": "c01", "approved": True}, {"id": "c02", "approved": False}]}
    render_candidates(cands, tmp_path, font_path=STHEITI)
    assert rendered == ["c01"]
    rendered.clear()
    render_candidates(cands, tmp_path, all_clips=True, font_path=STHEITI)
    assert rendered == ["c01", "c02"]


@pytest.mark.slow
def test_render_candidates_relative_out_dir(tmp_path, monkeypatch):
    # ffmpeg 在暫存目錄執行；相對 out_dir 也要落在呼叫者的工作目錄底下
    recording = tmp_path / "rec.wav"
    _write_sine_wav(recording, seconds=3)
    candidates = {"recording": str(recording), "rec_start": "x", "marvin_audio": "original", "marvin_delay": 0.3,
                  "clips": [{"id": "c01", "approved": True, "title": "t", "post_type": "名場面", "from": 0.0, "to": 2.0,
                             "events": [{"kind": "human", "speaker": "A", "from": 0.2, "to": 1.0, "text": "哈囉"}]}]}
    monkeypatch.chdir(tmp_path)
    render_candidates(candidates, Path("rel_out"), repo_root=tmp_path, font_path=STHEITI)
    assert (tmp_path / "rel_out" / "c01.mp4").exists()

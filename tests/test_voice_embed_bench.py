"""scripts/voice_embed_bench 的離線單元測試（只需 numpy + pytest；ffmpeg 測試無 ffmpeg 則 skip）。"""
import re
import shutil
import sqlite3
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from scripts.voice_embed_bench import audio_io, bench, metrics
from scripts.voice_embed_bench.augment import fit_length, mix_at_snr, pink_noise
from scripts.voice_embed_bench.corpus import ACTIONS, build_corpus, load_anchors, load_transcripts
from scripts.voice_embed_bench.synth_tts import PITCHES, RATES, plan_clips

HAN = re.compile(r"[一-鿿]")


def _rms(x):
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))


# ── 1. load_anchors ─────────────────────────────────────────────

def test_load_anchors_only_han():
    anchors = load_anchors()
    assert set(anchors) == set(ACTIONS)
    for action, texts in anchors.items():
        assert texts, action
        assert all(HAN.search(t) for t in texts)
    flat = [t for texts in anchors.values() for t in texts]
    assert "skip" not in flat and "pause" not in flat
    assert "下一首" in anchors["skip"]


# ── 2. build_corpus ─────────────────────────────────────────────

def test_build_corpus_dedup_labels_and_ids():
    phrases = {
        "positives": {"skip": ["下一首", "換別首"], "pause": ["先暫停"]},
        "negatives_near_miss": ["下一關很難"],
        "negatives_chat": ["今天好累"],
    }
    anchors = {"skip": ["下一首"], "pause": ["暫停音樂"]}
    corpus = build_corpus(phrases, anchors, transcripts=["晚餐吃什麼"])
    by_text = {c["text"]: c for c in corpus}

    assert [c["text"] for c in corpus].count("下一首") == 1
    assert by_text["下一首"]["kind"] == "anchor"
    assert by_text["下一首"]["label"] == "skip"
    assert by_text["換別首"]["kind"] == "paraphrase"
    assert by_text["下一關很難"]["label"] is None
    assert by_text["今天好累"]["label"] is None
    assert by_text["晚餐吃什麼"]["kind"] == "transcript"
    assert len(corpus) == 7
    for c in corpus:
        assert re.fullmatch(rf"{c['kind']}-[0-9a-f]{{10}}", c["id"])

    again = build_corpus(phrases, anchors, transcripts=["晚餐吃什麼"])
    assert [c["id"] for c in again] == [c["id"] for c in corpus]


# ── 3. load_transcripts ─────────────────────────────────────────

@pytest.fixture
def transcripts_db(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE transcripts(text TEXT)")
    rows = [
        "下一首啦", "暫停音樂好了", "今天好累", "a", "這是一個非常非常非常非常長的句子" * 3,
        "今天好累", " 今天好累 ", "晚餐吃什麼", "明天要上班",
    ]
    conn.executemany("INSERT INTO transcripts(text) VALUES (?)", [(r,) for r in rows])
    conn.commit()
    conn.close()
    return db


ANCHORS_T = {"skip": ["下一首"], "pause": ["暫停音樂"], "resume": ["繼續播"], "stop": ["停止播放"]}


def test_load_transcripts_filters_and_dedups(transcripts_db):
    got = load_transcripts(transcripts_db, limit=100, seed=0, anchors=ANCHORS_T)
    assert sorted(got) == sorted(["今天好累", "晚餐吃什麼", "明天要上班"])


def test_load_transcripts_limit_and_seed(transcripts_db):
    a = load_transcripts(transcripts_db, limit=2, seed=7, anchors=ANCHORS_T)
    b = load_transcripts(transcripts_db, limit=2, seed=7, anchors=ANCHORS_T)
    assert len(a) == 2
    assert a == b
    assert set(a) <= {"今天好累", "晚餐吃什麼", "明天要上班"}


# ── 4. plan_clips ───────────────────────────────────────────────

def test_plan_clips_order_and_format():
    items = [
        {"id": "anchor-aaaaaaaaaa", "text": "下一首", "label": "skip", "kind": "anchor"},
        {"id": "chat-bbbbbbbbbb", "text": "今天好累", "label": None, "kind": "chat"},
    ]
    voices = ("v1", "v2")
    clips = plan_clips(items, voices, seed=0)
    assert [c["clip_id"] for c in clips] == [
        "anchor-aaaaaaaaaa__v1", "anchor-aaaaaaaaaa__v2",
        "chat-bbbbbbbbbb__v1", "chat-bbbbbbbbbb__v2",
    ]
    assert clips[0]["path"] == "audio/clean/anchor-aaaaaaaaaa__v1.wav"
    assert clips[0]["item_id"] == "anchor-aaaaaaaaaa"
    assert clips[2]["label"] is None and clips[2]["kind"] == "chat"
    assert all(c["condition"] == "clean" for c in clips)
    assert all(c["rate"] in RATES and c["pitch"] in PITCHES for c in clips)
    assert plan_clips(items, voices, seed=0) == clips


def test_plan_clips_seed_changes_rate_pitch():
    items = [{"id": f"chat-{i:010d}", "text": "x", "label": None, "kind": "chat"} for i in range(20)]
    a = plan_clips(items, ("v1",), seed=0)
    b = plan_clips(items, ("v1",), seed=1)
    assert [(c["rate"], c["pitch"]) for c in a] != [(c["rate"], c["pitch"]) for c in b]


# ── 5/6. augment ────────────────────────────────────────────────

def _sine(n=16000, amp=0.3):
    return (amp * np.sin(2 * np.pi * 1000 * np.arange(n) / 16000)).astype(np.float32)


def test_pink_noise_rms():
    noise = pink_noise(16000, seed=0)
    assert noise.dtype == np.float32
    assert _rms(noise) == pytest.approx(0.1, abs=1e-3)


def test_mix_at_snr_hits_target():
    speech = _sine()
    noise = pink_noise(16000, seed=0)
    out = mix_at_snr(speech, noise, 10)
    assert len(out) == len(speech)
    snr = 20 * np.log10(_rms(speech) / _rms(out - speech))
    assert abs(snr - 10) < 0.05


def test_mix_at_snr_peak_normalized_when_clipping():
    speech = _sine(amp=0.9)
    noise = pink_noise(16000, seed=0)
    out = mix_at_snr(speech, noise, 0)
    assert np.max(np.abs(out)) == pytest.approx(1.0, abs=1e-5)
    # 整體縮放後 SNR 應保留：最小平方估回 speech 的縮放係數再量 SNR
    k = float(np.dot(out, speech) / np.dot(speech, speech))
    snr = 20 * np.log10(_rms(k * speech) / _rms(out - k * speech))
    assert abs(snr - 0) < 0.2


def test_mix_at_snr_short_noise_and_silence():
    speech = _sine(n=1000)
    out = mix_at_snr(speech, pink_noise(100, seed=1), 10)
    assert len(out) == 1000
    silent = mix_at_snr(np.zeros(500, dtype=np.float32), pink_noise(100, seed=1), 10)
    assert silent.shape == (500,) and not silent.any()


def test_fit_length_tiles_from_offset():
    out = fit_length(np.array([1.0, 2.0, 3.0]), 7, 2)
    assert out.tolist() == [3.0, 1.0, 2.0, 3.0, 1.0, 2.0, 3.0]


# ── 7. metrics ──────────────────────────────────────────────────

LABELS = ["skip", "pause", "resume", None, None]
SCORES = np.array([
    [0.9, 0.1, 0.2, 0.0],
    [0.1, 0.7, 0.2, 0.3],
    [0.2, 0.1, 0.6, 0.85],
    [0.3, 0.4, 0.55, 0.2],
    [0.1, 0.2, 0.3, 0.95],
])
ANCHOR_LABELS = ["skip", "pause", "resume", "stop"]


def test_predict():
    preds, best = metrics.predict(SCORES, ANCHOR_LABELS)
    assert preds == ["skip", "pause", "stop", "resume", "stop"]
    assert best.tolist() == [0.9, 0.7, 0.85, 0.55, 0.95]


def test_sweep_hand_computed():
    preds, best = metrics.predict(SCORES, ANCHOR_LABELS)
    rows = metrics.sweep(LABELS, preds, best, np.array([0.5, 0.8]))
    assert rows[0]["threshold"] == 0.5
    assert rows[0]["recall"] == pytest.approx(2 / 3)
    assert rows[0]["wrong_action"] == pytest.approx(1 / 3)
    assert rows[0]["fpr"] == pytest.approx(1.0)
    assert rows[1]["recall"] == pytest.approx(1 / 3)
    assert rows[1]["wrong_action"] == pytest.approx(1 / 3)
    assert rows[1]["fpr"] == pytest.approx(0.5)


def test_sweep_no_pos_or_neg_gives_none():
    rows = metrics.sweep([None, None], ["skip", "skip"], np.array([0.9, 0.1]), np.array([0.5]))
    assert rows[0]["recall"] is None and rows[0]["wrong_action"] is None
    assert rows[0]["fpr"] == pytest.approx(0.5)
    rows = metrics.sweep(["skip"], ["skip"], np.array([0.9]), np.array([0.5]))
    assert rows[0]["fpr"] is None and rows[0]["recall"] == 1.0


def test_top1():
    assert metrics.top1(LABELS, ["skip", "pause", "stop", "resume", "stop"]) == pytest.approx(2 / 3)
    assert metrics.top1([None], ["skip"]) is None


def test_operating_point():
    rows = [
        {"threshold": 0.1, "recall": 0.5, "wrong_action": 0.0, "fpr": 0.02},
        {"threshold": 0.2, "recall": 0.8, "wrong_action": 0.0, "fpr": 0.01},
        {"threshold": 0.3, "recall": 0.8, "wrong_action": 0.0, "fpr": 0.0},
        {"threshold": 0.4, "recall": 0.9, "wrong_action": 0.0, "fpr": 0.5},
        {"threshold": 0.5, "recall": 1.0, "wrong_action": 0.0, "fpr": None},
    ]
    assert metrics.operating_point(rows, 0.01)["threshold"] == 0.2
    assert metrics.operating_point(rows, 0.5)["threshold"] == 0.4
    assert metrics.operating_point(rows[:1] + rows[4:], 0.01) is None


def test_percentile_ms():
    assert metrics.percentile_ms([], 50) is None
    assert metrics.percentile_ms([0.1, 0.1, 0.1], 50) == pytest.approx(100.0)


# ── 8. peak_rss_mb ──────────────────────────────────────────────

@pytest.mark.parametrize("platform_name,raw,expected", [
    ("darwin", 1048576, 1.0),
    ("linux", 1024, 1.0),
])
def test_peak_rss_mb_units(monkeypatch, platform_name, raw, expected):
    monkeypatch.setattr(sys, "platform", platform_name)
    monkeypatch.setattr(bench.resource, "getrusage", lambda who: SimpleNamespace(ru_maxrss=raw))
    assert bench.peak_rss_mb() == pytest.approx(expected)


# ── 9. run_bench 端到端（假 encoder）────────────────────────────

ANCHORS_B = {"skip": ["下一首", "換歌"], "pause": ["暫停"], "resume": ["繼續播"], "stop": ["停止播放"]}
ACTION_IDX = {"skip": 0, "pause": 1, "resume": 2, "stop": 3}


class FakeEncoder:
    def encode_texts(self, texts):
        out = np.zeros((len(texts), 4), dtype=np.float32)
        for i, t in enumerate(texts):
            action = next(a for a, ts in ANCHORS_B.items() if t in ts)
            out[i, ACTION_IDX[action]] = 1.0
        return out

    def encode_audio(self, audio, sr):
        k = int(audio[0])
        if k < 0:
            return np.full(4, 0.5, dtype=np.float32)
        v = np.zeros(4, dtype=np.float32)
        v[k] = 1.0
        return v


def _clip(name, label, kind, first, cond="clean"):
    return ({"clip_id": name, "label": label, "kind": kind, "condition": cond,
             "path": f"audio/{cond}/{name}.wav"}, first)


def test_run_bench_end_to_end(tmp_path):
    specs = [
        _clip("p0", "skip", "anchor", 0), _clip("p1", "pause", "paraphrase", 1),
        _clip("p2", "resume", "anchor", 2), _clip("p3", "stop", "paraphrase", 3),
        _clip("n0", None, "chat", -1), _clip("n1", None, "near_miss", -1),
    ]
    clips = [c for c, _ in specs]
    firsts = {c["clip_id"]: f for c, f in specs}

    def fake_load(path):
        return np.array([firsts[path.stem], 0.0], dtype=np.float32), 16000

    res = bench.run_bench(FakeEncoder(), ANCHORS_B, clips, tmp_path, warmup=2, load_audio=fake_load)
    assert res["anchors"]["n"] == 5
    assert res["latency_ms"]["n"] == 4
    assert len(res["predictions"]) == 6
    cond = res["conditions"]["clean"]
    assert cond["n_pos"] == 4 and cond["n_neg"] == 2
    assert cond["top1"] == 1.0
    assert cond["op_fpr1"] is not None
    assert cond["op_fpr1"]["recall"] == 1.0
    assert cond["op_fpr1"]["threshold"] == pytest.approx(0.51)
    assert cond["top1_by_kind"] == {"anchor": 1.0, "paraphrase": 1.0}
    assert cond["fpr_by_kind_at_op1"] == {"chat": 0.0, "near_miss": 0.0}


# ── 10. render_report ───────────────────────────────────────────

def test_render_report_table():
    results = {
        "latency_ms": {"p50": 12.0, "p95": 20.0, "mean": 13.0, "n": 5},
        "conditions": {
            "clean": {"n_pos": 4, "n_neg": 2, "top1": 1.0,
                      "op_fpr1": {"threshold": 0.51, "recall": 1.0}, "op_fpr5": None},
            "opus": {"n_pos": 0, "n_neg": 2, "top1": None, "op_fpr1": None, "op_fpr5": None},
        },
    }
    report = bench.render_report(results)
    assert "| condition | n_pos | n_neg | top1 | thr@FPR≤1% | recall@FPR≤1% " in report
    assert "| clean | 4 | 2 | 1.000 | 0.510 | 1.000 | - | - |" in report
    assert "| opus | 0 | 2 | - | - | - | - | - |" in report


# ── 11. opus_roundtrip ──────────────────────────────────────────

@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要 ffmpeg")
def test_opus_roundtrip(tmp_path):
    src, dst = tmp_path / "in.wav", tmp_path / "out.wav"
    audio_io.write_wav(src, _sine(16000))
    audio_io.opus_roundtrip(src, dst)
    audio, sr = audio_io.read_wav(dst)
    assert sr == 16000
    assert abs(len(audio) - 16000) / 16000 < 0.05

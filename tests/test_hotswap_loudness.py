"""hotswap_loudness — loudnorm 量測解析 + stream2 音量匹配 filter（Slice 2）。

stream1 用動態 loudnorm 播整首；hotswap 的 stream2 若只用固定 volume，切換後
整首剩餘段落音量會跟原本不一致。解法：stream2 用 linear loudnorm（2-pass 量測
值 → 常數增益、同 -14 LUFS target、無暫態）。量測失敗則 fallback 固定 volume。
"""
from __future__ import annotations

import json

from hotswap_loudness import (
    build_stream2_music_filter, build_volume_swap_af, parse_loudnorm_measurement,
)


_GOOD_JSON = {
    "input_i": "-9.52", "input_tp": "-0.50", "input_lra": "5.40",
    "input_thresh": "-19.87", "output_i": "-14.00", "target_offset": "0.12",
}


def _stderr_with_json(d):
    # 模擬 ffmpeg loudnorm 把 JSON 印在 stderr 最後
    return "ffmpeg version ...\nsome log\n" + json.dumps(d) + "\n"


# ── parse ─────────────────────────────────────────────────────────────────────

def test_parse_extracts_required_measured_fields():
    m = parse_loudnorm_measurement(_stderr_with_json(_GOOD_JSON))
    assert m is not None
    assert m["input_i"] == "-9.52"
    assert m["input_tp"] == "-0.50"
    assert m["input_lra"] == "5.40"
    assert m["input_thresh"] == "-19.87"
    assert m["target_offset"] == "0.12"


def test_parse_returns_none_on_no_json():
    assert parse_loudnorm_measurement("just plain ffmpeg log, no json") is None


def test_parse_returns_none_on_malformed_json():
    assert parse_loudnorm_measurement("log { not valid json }") is None


def test_parse_returns_none_when_required_field_missing():
    """缺 input_thresh → 不能組 linear loudnorm，視為量測失敗。"""
    bad = dict(_GOOD_JSON)
    del bad["input_thresh"]
    assert parse_loudnorm_measurement(_stderr_with_json(bad)) is None


def test_parse_picks_last_json_block():
    """stderr 可能有多個 {}；取最後一個（loudnorm 結果在最後）。"""
    s = '{"input_i": "wrong"}\nmore log\n' + json.dumps(_GOOD_JSON)
    m = parse_loudnorm_measurement(s)
    assert m["input_i"] == "-9.52"


# ── filter ────────────────────────────────────────────────────────────────────

def test_filter_uses_linear_loudnorm_when_measured():
    fc = build_stream2_music_filter(_GOOD_JSON, vol=0.10)
    assert "loudnorm" in fc
    assert "linear=true" in fc
    assert "measured_I=-9.52" in fc
    assert "measured_thresh=-19.87" in fc
    assert "offset=0.12" in fc
    assert "volume=0.100" in fc          # loudnorm 後仍套串流音量
    assert fc.startswith("[1:a]")
    assert fc.endswith("[music]")


def test_filter_falls_back_to_volume_when_no_measurement():
    """量測沒好（None）→ 固定 volume（Slice 1 行為），不該炸。"""
    fc = build_stream2_music_filter(None, vol=0.10)
    assert "loudnorm" not in fc
    assert "volume=0.100" in fc
    assert fc == "[1:a]volume=0.100[music]"


def test_filter_no_transient_no_afade():
    """linear loudnorm 是常數增益、無暫態；filter 不該含 afade（實聽證實 afade 放大爆音）。"""
    fc = build_stream2_music_filter(_GOOD_JSON, vol=0.10)
    assert "afade" not in fc


# ── volume swap -af（語音調音量即時生效，stream2 純換音量、無 TTS）────────────────

def test_volume_swap_af_uses_linear_loudnorm_when_measured():
    """有量測 → linear loudnorm 匹配 + 新音量。單輸入 -af，無 [1:a]/[music] 標籤。"""
    af = build_volume_swap_af(_GOOD_JSON, vol=0.20)
    assert "loudnorm" in af
    assert "linear=true" in af
    assert "measured_I=-9.52" in af
    assert "measured_thresh=-19.87" in af
    assert "offset=0.12" in af
    assert "volume=0.200" in af
    assert "[1:a]" not in af
    assert "[music]" not in af


def test_volume_swap_af_keeps_dynamic_loudnorm_when_no_measurement():
    """量測沒好 → 仍保 dynamic loudnorm（對齊 stream1 line 7081），不像 TTS 路徑裸 volume。

    音量 swap 無 ducking 遮接縫，stream2 必須跟 stream1 同響度行為，故 fallback 保 loudnorm。
    """
    af = build_volume_swap_af(None, vol=0.20)
    assert "loudnorm=I=-14:TP=-1.5:LRA=11" in af
    assert "volume=0.200" in af
    assert "linear=true" not in af


def test_volume_swap_af_no_afade():
    af = build_volume_swap_af(_GOOD_JSON, vol=0.10)
    assert "afade" not in af

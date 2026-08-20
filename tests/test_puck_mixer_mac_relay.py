"""TDD：device/puck_mixer.py::audio_stream_url()——Pi 端組出 Mac /audio_stream
的完整網址（含 token）。

2026-08-20：換歌決策/DJ口白搬回 Mac 端 mixer，Pi 不再自己 resolve 個別歌曲的
YouTube 網址（舊版 resolve_stream_url()/`/puck_deck` 轉發整條路已拿掉，見
device/puck_mixer.py 模組說明）——現在只有一個固定端點要組：Mac 那顆 mixer
連續廣播出來的 /audio_stream，ffmpeg 直接對這個 URL 讀，跟讀一般 HTTP 檔案
完全一樣。
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import importlib

import device.puck_mixer as puck_mixer


def test_audio_stream_url_builds_url_with_token(monkeypatch):
    monkeypatch.setattr(puck_mixer, "MAC_BASE_URL", "http://100.123.68.86:8790")
    monkeypatch.setattr(puck_mixer, "MAC_TOKEN", "s3cret")

    out = puck_mixer.audio_stream_url()

    parsed = urlparse(out)
    assert parsed.scheme == "http"
    assert parsed.netloc == "100.123.68.86:8790"
    assert parsed.path == "/audio_stream"
    qs = parse_qs(parsed.query)
    assert qs["t"] == ["s3cret"]


def test_audio_stream_url_omits_token_param_when_no_token(monkeypatch):
    monkeypatch.setattr(puck_mixer, "MAC_BASE_URL", "http://100.123.68.86:8790")
    monkeypatch.setattr(puck_mixer, "MAC_TOKEN", None)

    out = puck_mixer.audio_stream_url()

    qs = parse_qs(urlparse(out).query)
    assert "t" not in qs


def test_mac_base_url_default_targets_satellite_port():
    """⚠️ 2026-08-18 事故回歸測試：MAC_BASE_URL 必須指回 satellite 進程的 8790，
    不是獨立 port——car-presence 心跳/決策車puck播放都該由 satellite（同一個
    進程）接手，見 puck_mixer.py 模組頂部說明。這裡鎖住預設值必須是 8790，
    避免以後又「順手」換回獨立 port 繞遠路。"""
    assert "8790" in puck_mixer.MAC_BASE_URL


def test_mac_token_env_defaults_fall_back_to_marvin_vol_token(monkeypatch):
    """部署慣例：MARVIN_MAC_TOKEN 沒設就沿用 MARVIN_VOL_TOKEN（跟 systemd unit 對齊
    MARVIN_TEXT_TOKEN 的既有慣例一致，見 volume_server.py 的 MAC_SAY/TOKEN 用法）。"""
    monkeypatch.delenv("MARVIN_MAC_TOKEN", raising=False)
    monkeypatch.setenv("MARVIN_VOL_TOKEN", "deploy-token")
    reloaded = importlib.reload(puck_mixer)
    try:
        assert reloaded.MAC_TOKEN == "deploy-token"
    finally:
        importlib.reload(puck_mixer)   # 還原成正常環境，避免污染其他測試


def test_fetch_car_now_title_returns_title_when_playing(monkeypatch):
    import io
    import json

    class _FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    body = json.dumps({"playing": True, "title": "現正播放的歌"}).encode()
    monkeypatch.setattr(puck_mixer.urllib.request, "urlopen", lambda url, timeout=3.0: _FakeResp(body))

    assert puck_mixer.fetch_car_now_title() == "現正播放的歌"


def test_fetch_car_now_title_returns_none_when_not_playing(monkeypatch):
    import io
    import json

    class _FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    body = json.dumps({"playing": False}).encode()
    monkeypatch.setattr(puck_mixer.urllib.request, "urlopen", lambda url, timeout=3.0: _FakeResp(body))

    assert puck_mixer.fetch_car_now_title() is None


def test_fetch_car_now_title_returns_none_on_connection_failure(monkeypatch):
    def _boom(url, timeout=3.0):
        raise OSError("連不到 Mac")
    monkeypatch.setattr(puck_mixer.urllib.request, "urlopen", _boom)

    assert puck_mixer.fetch_car_now_title() is None

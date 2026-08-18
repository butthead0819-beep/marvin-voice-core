"""TDD：device/puck_mixer.py::resolve_stream_url() 改走 Mac /puck_deck 轉發
（2026-08-18，跟 ESP32 edge端混音同一條路，取代 Pi 本地 yt-dlp+cookies）。

背景：Pi 本地 resolve 先後踩兩個坑（見 incident_youtube_403_ip_throttle_2026-08-17
記憶）——無 cookies 被 IP 節流 403、接上 cookies 後 deno 解 JS challenge 在 Pi
弱 CPU 上常吃到 ~24s，還有 cookies session 跟 Mac 共用會被 rotate 掉的風險。
改成讓 Mac 用自己已驗證穩定的 cookiesfrombrowser session 現場 resolve+轉碼，
Pi 只當純消費端接 HTTP 串流；resolve_stream_url() 不再做任何網路呼叫，只組字串。
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import importlib

import device.puck_mixer as puck_mixer


def test_resolve_stream_url_builds_puck_deck_relay_url(monkeypatch):
    monkeypatch.setattr(puck_mixer, "MAC_BASE_URL", "http://100.123.68.86:8790")
    monkeypatch.setattr(puck_mixer, "MAC_TOKEN", "s3cret")

    out = puck_mixer.resolve_stream_url("https://www.youtube.com/watch?v=abc123")

    parsed = urlparse(out)
    assert parsed.scheme == "http"
    assert parsed.netloc == "100.123.68.86:8790"
    assert parsed.path == "/puck_deck"
    qs = parse_qs(parsed.query)
    assert qs["url"] == ["https://www.youtube.com/watch?v=abc123"]
    assert qs["t"] == ["s3cret"]


def test_resolve_stream_url_omits_token_param_when_no_token(monkeypatch):
    monkeypatch.setattr(puck_mixer, "MAC_BASE_URL", "http://100.123.68.86:8790")
    monkeypatch.setattr(puck_mixer, "MAC_TOKEN", None)

    out = puck_mixer.resolve_stream_url("https://www.youtube.com/watch?v=abc123")

    qs = parse_qs(urlparse(out).query)
    assert "t" not in qs


def test_mac_base_url_default_targets_satellite_port():
    """⚠️ 2026-08-18 事故回歸測試（兩階段）：v1 曾讓 24/7 Discord bot 自己開一個
    獨立小 app 服務 /puck_deck，port 一度沿用 satellite 的 8790、撞上另一個
    常駐進程（browsersatellite），整支 bot 啟動時無聲掛掉；改到獨立 port 8792
    後又發現「由 24/7 bot 決策車puck播放」這個方向本身不對——car-presence 心跳
    打的是 satellite 的 :8790，車puck的「在場觸發」該由 satellite（跟 ESP32
    同一個進程）接手，不是 24/7 Discord bot。最終定案：MAC_BASE_URL 指回
    satellite 的 8790，改用 MARVIN_CAR_HARDWARE 只在 satellite 進程生效（其餘
    進程明確 override 關掉）確保只有一個決策者，而不是靠換 port 迴避。這裡鎖住
    預設值必須是 8790，避免以後又「順手」換回獨立 port 繞遠路。"""
    assert "8790" in puck_mixer.MAC_BASE_URL


def test_resolve_stream_url_makes_no_subprocess_calls(monkeypatch):
    """不再跑本地 yt-dlp——subprocess.run 完全不該被呼叫。"""
    calls = []
    monkeypatch.setattr(puck_mixer.subprocess, "run", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(puck_mixer, "MAC_BASE_URL", "http://100.123.68.86:8790")
    monkeypatch.setattr(puck_mixer, "MAC_TOKEN", None)

    puck_mixer.resolve_stream_url("https://www.youtube.com/watch?v=abc123")

    assert calls == []


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

"""/mic_ws 路由只在 MARVIN_PUCK_MIC=1 時註冊（見 car_http_app.build_text_app 尾端）。

build_text_app 是純 wiring、不起 server（見其 docstring），直接檢查
app.router 有沒有這條路由即可，不用真的連線。
"""
from unittest.mock import MagicMock


def _make_vc():
    vc = MagicMock()
    return vc


def _has_mic_ws_route(app) -> bool:
    return any(r.resource is not None and r.resource.canonical == "/mic_ws" for r in app.router.routes())


def test_mic_ws_route_absent_by_default(monkeypatch):
    monkeypatch.delenv("MARVIN_PUCK_MIC", raising=False)
    from car_http_app import build_text_app
    app = build_text_app(_make_vc())
    assert _has_mic_ws_route(app) is False


def test_mic_ws_route_present_when_enabled(monkeypatch):
    monkeypatch.setenv("MARVIN_PUCK_MIC", "1")
    from car_http_app import build_text_app
    app = build_text_app(_make_vc())
    assert _has_mic_ws_route(app) is True

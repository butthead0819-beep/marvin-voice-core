"""TDD: 車puck紅燈自動修復 — Tailscale Funnel對外ingress卡住偵測+自癒。

見 scripts/funnel_healthcheck.py 開頭說明：健康→不動；單次失敗重測一次濾掉
瞬斷；連兩次才觸發 heal_fn；heal 後再驗一次決定 healed/heal_failed。
只測純邏輯（decide_and_act 用假 check_fn/heal_fn 注入），不碰真的
dig/curl/tailscale/網路。
"""
from __future__ import annotations

from scripts.funnel_healthcheck import decide_and_act


def _counting_check(results):
    """回傳一個 check_fn：每次呼叫從 results 依序吐一筆 (ok, detail)。"""
    it = iter(results)

    def _check():
        return next(it)
    return _check


def test_healthy_on_first_check_skips_retry_and_heal():
    heal_calls = []
    check = _counting_check([(True, "HTTP 200")])

    result = decide_and_act(check, lambda: heal_calls.append(1), retry_sleep_fn=lambda s: None)

    assert result == {"action": "healthy", "detail": "HTTP 200"}
    assert heal_calls == []


def test_transient_failure_recovers_on_retry_without_healing():
    heal_calls = []
    check = _counting_check([(False, "curl exit=35"), (True, "HTTP 200")])

    result = decide_and_act(check, lambda: heal_calls.append(1), retry_sleep_fn=lambda s: None)

    assert result["action"] == "transient"
    assert "HTTP 200" in result["detail"]
    assert heal_calls == []   # 瞬斷不該觸發 heal


def test_persistent_failure_triggers_heal_and_recovers():
    heal_calls = []
    check = _counting_check([
        (False, "curl exit=35"),   # 第一次失敗
        (False, "curl exit=35"),   # 重測仍失敗 → 觸發 heal
        (True, "HTTP 200"),        # heal 後再驗 → 恢復
    ])

    result = decide_and_act(check, lambda: heal_calls.append(1), retry_sleep_fn=lambda s: None)

    assert result == {"action": "healed", "detail": "自癒後恢復: HTTP 200"}
    assert heal_calls == [1]


def test_persistent_failure_survives_heal_reports_heal_failed():
    heal_calls = []
    check = _counting_check([
        (False, "curl exit=35"),
        (False, "curl exit=35"),
        (False, "curl exit=35"),   # heal 後仍失敗
    ])

    result = decide_and_act(check, lambda: heal_calls.append(1), retry_sleep_fn=lambda s: None)

    assert result["action"] == "heal_failed"
    assert heal_calls == [1]   # 只 heal 一次，不無限重試


def test_resolve_public_ip_uses_external_dns_not_local():
    """必須用 @8.8.8.8 查，不能倚賴本機/MagicDNS resolver（假陽性陷阱，見檔頭）。"""
    from scripts.funnel_healthcheck import resolve_public_ip

    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        class R:
            stdout = "1.2.3.4\n"
        return R()

    ip = resolve_public_ip("example.ts.net", run_fn=fake_run)
    assert ip == "1.2.3.4"
    assert "@8.8.8.8" in captured["cmd"]


def test_resolve_public_ip_returns_none_when_dig_fails():
    from scripts.funnel_healthcheck import resolve_public_ip

    def fake_run(cmd, **kw):
        raise RuntimeError("dig not found")

    assert resolve_public_ip("example.ts.net", run_fn=fake_run) is None


def test_probe_public_tls_forces_resolve_to_given_public_ip():
    """驗證 curl 呼叫真的帶 --resolve 強制走公網 IP，不是讓系統 DNS 決定。"""
    from scripts.funnel_healthcheck import probe_public_tls

    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        class R:
            returncode = 0
            stdout = "200"
            stderr = ""
        return R()

    ok, detail = probe_public_tls("host.ts.net", "9.9.9.9", "tok", run_fn=fake_run)
    assert ok is True
    assert "200" in detail
    assert "host.ts.net:443:9.9.9.9" in " ".join(captured["cmd"])


def test_probe_public_tls_connection_failure_is_reported():
    from scripts.funnel_healthcheck import probe_public_tls

    def fake_run(cmd, **kw):
        class R:
            returncode = 35
            stdout = ""
            stderr = "SSL_ERROR_SYSCALL"
        return R()

    ok, detail = probe_public_tls("host.ts.net", "9.9.9.9", "tok", run_fn=fake_run)
    assert ok is False
    assert "35" in detail

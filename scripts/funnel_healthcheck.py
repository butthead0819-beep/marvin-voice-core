#!/usr/bin/env python3
"""車puck紅燈自動修復 — Tailscale Funnel對外ingress卡住偵測+自癒。

見 project_car_puck_funnel_tls_and_fallback_2026-07-26 記憶：車puck開機紅燈
（LED_ERROR，sticky不會自動熄）根因是 Tailscale Funnel 的 anycast ingress
節點對外 registration 卡住，外部 TLS 連線在 ClientHello 後直接被斷——
跟 ESP32 韌體無關，純 Tailscale 服務端問題。修法是 `tailscale funnel reset`
清掉卡住狀態，再重新套用同一組 config，ingress 重新註冊後立刻恢復。
2026-08-15 第二次踩到同個問題，這次補上自動偵測+自癒，不用再手動跑。

⚠️ 量測陷阱：在這台跑 tailscaled 的 Mac 上直接 curl/openssl 測 *.ts.net
會被 MagicDNS 解析成內網 tailnet IP、直接走 WireGuard 內網連線，完全繞過
Funnel 的公網 ingress——這樣測到的「健康」是假陽性，測不出 ESP32／真實
外部裝置打不到的問題。必須先用外部 DNS（8.8.8.8）解析出真公網 IP，
再用 curl --resolve 強制連那個 IP，才是真正驗到公網路徑。

由 launchd（com.antigravity.marvin.funnelhealth，每 10 分）執行，與主程序
獨立——跟 scripts/pipeline_heartbeat_probe.py 同一種「防線」設計：獨立
進程、告警走 Discord REST 直打（不依賴可能已經半死的 bot 進程）、同 signature
6h 內不重發。

kill-switch：env MARVIN_FUNNEL_HEALTHCHECK=0 → 直接退出（不檢查、不修復）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from scripts.pipeline_heartbeat_probe import decide_alert, dm_owner  # noqa: E402
STATE_PATH = REPO / "records" / "funnel_health_state.json"
FUNNEL_HOST = "macbook-air.tail7ba8d0.ts.net"
FUNNEL_PORT = 8790
REALERT_AFTER_S = 6 * 3600
TRANSIENT_RETRY_DELAY_S = 5
HEAL_PROPAGATE_WAIT_S = 20


# ── 純邏輯（好測，不碰真網路/subprocess）─────────────────────────────────────

def resolve_public_ip(host: str, *, run_fn=subprocess.run) -> str | None:
    """`dig @8.8.8.8 <host> +short` 拿真公網 IP（不用本機 DNS，避免 MagicDNS 攔截）。"""
    try:
        r = run_fn(["dig", "+short", "@8.8.8.8", host],
                    capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    return lines[0] if lines else None


def probe_public_tls(host: str, public_ip: str, token: str,
                      *, path: str = "/now", timeout: int = 10,
                      run_fn=subprocess.run) -> tuple[bool, str]:
    """強制打真公網 IP 測 TLS 握手＋HTTP 回應——這是 ESP32/外部裝置實際會走的路徑。

    Funnel 對外一律是標準 HTTPS 443（`port` 參數是本機被 proxy 的埠，跟這裡
    連的公網埠是兩回事，見 heal_funnel）。只要連線層成功、拿得到 HTTP 狀態碼
    （不管是 200 還是 401）就算「公網路徑通」；這裡驗的是 ingress 是否卡住，
    不是 token 對不對。
    """
    url = f"https://{host}{path}?t={token}"
    try:
        r = run_fn(
            ["curl", "-sS", "-m", str(timeout), "--resolve", f"{host}:443:{public_ip}",
             "-o", "/dev/null", "-w", "%{http_code}", url],
            capture_output=True, text=True, timeout=timeout + 5,
        )
    except Exception as e:
        return False, f"curl 執行失敗: {type(e).__name__}: {e}"
    if r.returncode != 0:
        return False, f"curl exit={r.returncode}（{(r.stderr or '').strip()[:150]}）— 公網 ingress 連不上"
    code = (r.stdout or "").strip()
    if not code.isdigit():
        return False, f"curl 沒回 HTTP 狀態碼: {r.stdout!r}"
    return True, f"HTTP {code}"


def check_funnel_public(host: str, token: str,
                         *, resolve_fn=resolve_public_ip,
                         probe_fn=probe_public_tls) -> tuple[bool, str]:
    ip = resolve_fn(host)
    if not ip:
        return False, f"DNS resolve {host} 失敗（@8.8.8.8）"
    return probe_fn(host, ip, token)


def heal_funnel(port: int, *, run_fn=subprocess.run, sleep_fn=time.sleep) -> None:
    """`tailscale funnel reset` 清掉卡住的 ingress 狀態，重新套用同一組設定。"""
    run_fn(["tailscale", "funnel", "reset"], capture_output=True, timeout=15)
    sleep_fn(1)
    run_fn(["tailscale", "funnel", "--bg", str(port)], capture_output=True, timeout=15)
    sleep_fn(HEAL_PROPAGATE_WAIT_S)


def decide_and_act(check_fn, heal_fn, *, retry_sleep_fn=time.sleep) -> dict:
    """核心決策：健康→不動；單次失敗先重測一次濾掉瞬斷；連兩次才真的觸發自癒。

    回傳 {'action': 'healthy'|'transient'|'healed'|'heal_failed', 'detail': str}，
    純邏輯、不碰 Discord/launchd，方便注入假 check_fn/heal_fn 測試。
    """
    ok, detail = check_fn()
    if ok:
        return {"action": "healthy", "detail": detail}
    retry_sleep_fn(TRANSIENT_RETRY_DELAY_S)
    ok2, detail2 = check_fn()
    if ok2:
        return {"action": "transient", "detail": f"重測後恢復: {detail2}（首次失敗: {detail}）"}
    heal_fn()
    ok3, detail3 = check_fn()
    if ok3:
        return {"action": "healed", "detail": f"自癒後恢復: {detail3}"}
    return {"action": "heal_failed", "detail": f"自癒後仍失敗: {detail3}"}


# ── IO shell ─────────────────────────────────────────────────────────────────

def _read_token() -> str:
    tok = os.environ.get("MARVIN_TEXT_TOKEN", "")
    if tok:
        return tok
    for line in (REPO / ".env").read_text().splitlines():
        if line.startswith("MARVIN_TEXT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("MARVIN_TEXT_TOKEN not found")


def main() -> int:
    if os.environ.get("MARVIN_FUNNEL_HEALTHCHECK", "1") == "0":
        print("[funnel_health] kill-switch off, exit")
        return 0

    token = _read_token()

    def _check() -> tuple[bool, str]:
        return check_funnel_public(FUNNEL_HOST, token)

    def _heal() -> None:
        heal_funnel(FUNNEL_PORT)

    result = decide_and_act(_check, _heal)
    action, detail = result["action"], result["detail"]

    failures = [] if action in ("healthy", "transient") else [(action, detail)]
    alert_action = decide_alert(STATE_PATH, failures, realert_after_s=REALERT_AFTER_S)
    stamp = time.strftime("%m-%d %H:%M")

    if alert_action == "alert":
        emoji = "🔧" if action == "healed" else "🚨"
        verb = "已自動修復" if action == "healed" else "自動修復失敗，需要人工介入"
        print(f"[funnel_health] ALERT ({action}): {detail}")
        dm_owner(f"{emoji} [FunnelHealth {stamp}] 車puck公網連線異常，{verb}：\n{detail}")
    elif alert_action == "recovered":
        print("[funnel_health] recovered")
        dm_owner(f"✅ [FunnelHealth {stamp}] Funnel 公網連線已恢復正常。")
    else:
        print(f"[funnel_health] {action}（{alert_action}）: {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

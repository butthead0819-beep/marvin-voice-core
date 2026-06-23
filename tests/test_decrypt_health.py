"""DecryptHealthMonitor：偵測 secret_key desync「收到封包卻持續解不開」風暴。

純邏輯（無 IO / 無時鐘），now 由 caller 傳入 → 完全可單測。
驗證來源：2026-06-23 incident——14:29 網路斷線快速 RESUME 後接收金鑰 desync、
KeySync 重抓 key 無用（key 本身壞）、Sentinel 看不到傳輸層 CryptoError → 炸 40 分沒自癒。
"""
from decrypt_health import DecryptHealthMonitor


def test_no_escalate_below_min_failures():
    """失敗次數 < min_failures（封包還沒確認在穩定流進）→ 不升級。"""
    m = DecryptHealthMonitor(sustained_s=8.0, min_failures=10)
    for i in range(5):
        m.record_failure(now=float(i))
    assert m.should_escalate(now=5.0) is False


def test_no_escalate_if_burst_not_sustained():
    """夠多次但時間跨度 < sustained_s（瞬間爆量、KeySync 可能還救得回）→ 先不升級。"""
    m = DecryptHealthMonitor(sustained_s=8.0, min_failures=10)
    for i in range(20):
        m.record_failure(now=i * 0.1)   # 20 次只跨 1.9s
    assert m.should_escalate(now=1.9) is False


def test_escalate_on_sustained_zero_decrypt():
    """≥min_failures 次且持續 ≥sustained_s 秒零成功解密 → 升級（真 desync 風暴）。"""
    m = DecryptHealthMonitor(sustained_s=8.0, min_failures=10)
    for i in range(15):
        m.record_failure(now=float(i))   # 15 次跨 14s
    assert m.should_escalate(now=14.0) is True


def test_success_resets_streak():
    """中間有成功解密（key 自己同步回來了）→ 重置 streak、不升級。"""
    m = DecryptHealthMonitor(sustained_s=8.0, min_failures=10)
    for i in range(15):
        m.record_failure(now=float(i))
    m.record_success(now=15.0)
    assert m.should_escalate(now=16.0) is False


def test_escalate_fires_once_until_success():
    """升級後不重複轟炸（避免 spam orchestrate_recovery），要等成功解密才能再升級。"""
    m = DecryptHealthMonitor(sustained_s=8.0, min_failures=10)
    for i in range(15):
        m.record_failure(now=float(i))
    assert m.should_escalate(now=14.0) is True      # 第一次
    m.record_failure(now=15.0)
    assert m.should_escalate(now=30.0) is False     # 不重複
    # 一次成功解密（恢復）後，新一波 storm 仍可再升級
    m.record_success(now=31.0)
    for i in range(32, 47):
        m.record_failure(now=float(i))
    assert m.should_escalate(now=46.0) is True

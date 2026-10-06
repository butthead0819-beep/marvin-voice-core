"""DecryptHealthMonitor：偵測 secret_key desync「收到封包卻持續解不開」風暴。

純邏輯（無 IO / 無時鐘），now 由 caller 傳入 → 完全可單測。
驗證來源：2026-06-23 incident——14:29 網路斷線快速 RESUME 後接收金鑰 desync、
KeySync 重抓 key 無用（key 本身壞）、Sentinel 看不到傳輸層 CryptoError → 炸 40 分沒自癒。
"""
from decrypt_health import DecryptAttribution, DecryptHealthMonitor


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


def test_attrib_ok_only_never_emits():
    """只有 ok 沒有 fail → 即使遠超視窗也不吐（不清統計）。"""
    a = DecryptAttribution(window_s=60.0)
    for i in range(10):
        a.record(111, True, now=float(i), uid=42)
    assert a.summary(now=1000.0) is None


def test_attrib_emits_after_window_with_fail():
    """有 fail 但未滿視窗 → None；滿視窗 → 吐字串，含 ssrc/fail/uid。"""
    a = DecryptAttribution(window_s=60.0)
    for i in range(3):
        a.record(111, False, now=float(i), uid=42)
    assert a.summary(now=30.0) is None
    s = a.summary(now=60.0)
    assert s is not None
    assert "ssrc=111" in s
    assert "fail=3" in s
    assert "uid=42" in s


def test_attrib_clears_after_emit():
    """emit 後統計清空：緊接著再 summary → None。"""
    a = DecryptAttribution(window_s=60.0)
    a.record(111, False, now=0.0, uid=42)
    assert a.summary(now=60.0) is not None
    assert a.summary(now=1000.0) is None


def test_attrib_force_skips_window():
    """force=True 不用等視窗。"""
    a = DecryptAttribution(window_s=60.0)
    a.record(111, False, now=0.0, uid=42)
    assert a.summary(now=1.0, force=True) is not None


def test_attrib_sorts_by_fail_desc_and_lists_ok_only():
    """依 fail 由大到小；ok-only 的 ssrc 也列出，且 len 寫 -。"""
    a = DecryptAttribution(window_s=60.0)
    for i in range(5):
        a.record(222, False, now=float(i))
    for i in range(2):
        a.record(111, False, now=float(i))
    for i in range(4):
        a.record(333, True, now=float(i))
    s = a.summary(now=60.0)
    assert s.index("ssrc=222") < s.index("ssrc=111") < s.index("ssrc=333")
    seg_333 = s[s.index("ssrc=333"):]
    assert "ok=4" in seg_333
    assert "len=-" in seg_333


def test_attrib_features_only_from_failures():
    """pt/len 只收失敗封包；ok 封包的 payload 不進 pt。"""
    a = DecryptAttribution(window_s=60.0)
    a.record(111, False, now=0.0, payload=120, size=60)
    a.record(111, False, now=1.0, payload=78, size=200)
    a.record(111, True, now=2.0, payload=99, size=5)
    s = a.summary(now=60.0)
    assert "pt=[78, 120]" in s
    assert "len=60-200" in s
    assert "99" not in s


def test_attrib_max_ssrcs_truncates_with_count():
    """max_ssrcs=2 時 4 個有 fail 的 ssrc → 只列 2 段，尾端 …+2 ssrc。"""
    a = DecryptAttribution(window_s=60.0, max_ssrcs=2)
    for ssrc in (101, 102, 103, 104):
        a.record(ssrc, False, now=0.0)
    s = a.summary(now=60.0)
    assert s.count("ssrc=") == 2
    assert s.endswith("…+2 ssrc")


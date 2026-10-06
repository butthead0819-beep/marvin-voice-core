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


def test_attrib_clean_window_is_silent():
    """只有 ok 的窗口 → 到期也不吐（乾淨窗口默默存成基準）。"""
    a = DecryptAttribution(window_s=60.0)
    for i in range(10):
        a.record(111, True, now=float(i), uid=42)
    assert a.summary(now=59.0) is None
    assert a.summary(now=1000.0) is None


def test_attrib_baseline_then_anomaly_prints_baseline_first():
    """窗口1乾淨存基準 → 窗口2 有 fail → 先印基準再印異常；ok/fail 特徵分開列。"""
    a = DecryptAttribution(window_s=60.0)
    for i in range(60):
        a.record(111, True, now=float(i), payload=120, size=100, sample="aa")
    assert a.summary(now=60.0) is None
    for i in range(60, 120):
        a.record(111, False, now=float(i), payload=101, size=1000, sample="bb")
    s = a.summary(now=120.0)
    assert s.startswith("[異常前基準] ")
    base, anom = s.split(" ‖ [異常] ")
    assert "pt=[120]" in base
    assert "hdr=aa" in base
    assert anom.split(" fail=")[1].startswith("60(pt=[101]")
    assert "hdr=bb" in anom


def test_attrib_persistent_anomaly_has_no_baseline_prefix():
    """異常持續一窗 → 前綴只是 [異常]，不再重印基準。"""
    a = DecryptAttribution(window_s=60.0)
    for i in range(60):
        a.record(111, True, now=float(i), payload=120, size=100, sample="aa")
    assert a.summary(now=60.0) is None
    for i in range(60, 120):
        a.record(111, False, now=float(i), payload=101, size=1000, sample="bb")
    assert a.summary(now=120.0).startswith("[異常前基準] ")
    for i in range(120, 180):
        a.record(111, False, now=float(i), payload=101, size=1000, sample="cc")
    s = a.summary(now=180.0)
    assert s.startswith("[異常] ")
    assert "異常前基準" not in s


def test_attrib_recovery_then_clean_is_silent():
    """異常後第一個乾淨窗 → [恢復後]；再下一個乾淨窗 → None。"""
    a = DecryptAttribution(window_s=60.0)
    for i in range(60):
        a.record(111, True, now=float(i), payload=120, size=100, sample="aa")
    a.summary(now=60.0)
    for i in range(60, 120):
        a.record(111, False, now=float(i), payload=101, size=1000, sample="bb")
    assert a.summary(now=120.0) is not None
    for i in range(120, 180):
        a.record(111, True, now=float(i), payload=120, size=100, sample="aa")
    s = a.summary(now=180.0)
    assert s.startswith("[恢復後] ")
    for i in range(180, 240):
        a.record(111, True, now=float(i), payload=120, size=100, sample="aa")
    assert a.summary(now=240.0) is None


def test_attrib_first_anomaly_without_baseline():
    """從沒有乾淨窗口就異常 → 基準填 (無)。"""
    a = DecryptAttribution(window_s=60.0)
    for i in range(60):
        a.record(111, False, now=float(i), payload=101, size=1000, sample="bb")
    s = a.summary(now=60.0)
    assert s.startswith("[異常前基準] (無) ‖ [異常] ")


def test_attrib_force_on_clean_window_does_not_break_rolling():
    """force 且無 fail → None 且不清統計；之後到期仍正常滾入基準。"""
    a = DecryptAttribution(window_s=60.0)
    for i in range(10):
        a.record(111, True, now=float(i), payload=120, size=100, sample="aa")
    assert a.summary(now=5.0, force=True) is None
    assert a.summary(now=60.0) is None
    for i in range(60, 70):
        a.record(111, False, now=float(i), payload=101, size=1000, sample="bb")
    s = a.summary(now=120.0)
    base = s.split(" ‖ [異常] ")[0]
    assert "ok=10(pt=[120]" in base


def test_attrib_ok_and_fail_features_separate():
    """同 ssrc：ok 與 fail 各自的 pt/len 不互相污染。"""
    a = DecryptAttribution(window_s=60.0)
    a.record(111, True, now=0.0, payload=120, size=100, sample="aa")
    a.record(111, False, now=1.0, payload=101, size=1000, sample="bb")
    s = a.summary(now=60.0)
    ok_part = s.split("ok=")[1].split(" fail=")[0]
    fail_part = s.split(" fail=")[1]
    assert ok_part.startswith("1(pt=[120]")
    assert "len=100-100" in ok_part
    assert fail_part.startswith("1(pt=[101]")
    assert "len=1000-1000" in fail_part


def test_attrib_hdr_keeps_first_sample_only():
    """hdr 只記每窗第一個非 None 的樣本，第二個不覆蓋。"""
    a = DecryptAttribution(window_s=60.0)
    a.record(111, False, now=0.0, sample="aa")
    a.record(111, False, now=1.0, sample="bb")
    s = a.summary(now=60.0)
    assert "hdr=aa" in s
    assert "hdr=bb" not in s


def test_attrib_emits_after_window_with_fail():
    """有 fail 但未滿視窗 → None；滿視窗 → 吐字串，含 ssrc/fail/uid。"""
    a = DecryptAttribution(window_s=60.0)
    for i in range(3):
        a.record(111, False, now=float(i), uid=42)
    assert a.summary(now=30.0) is None
    s = a.summary(now=60.0)
    assert s is not None
    assert "ssrc=111 uid=42" in s
    assert " fail=3(" in s


def test_attrib_clears_after_emit():
    """emit 後統計清空：之後的乾淨窗口只看新樣本（fail=0，舊 fail 不殘留）。"""
    a = DecryptAttribution(window_s=60.0)
    a.record(111, False, now=0.0, uid=42)
    assert a.summary(now=60.0) is not None
    a.record(111, True, now=61.0)
    s = a.summary(now=121.0)
    assert s.startswith("[恢復後] ")
    assert "ok=1(" in s
    assert "fail=0" in s


def test_attrib_force_skips_window():
    """force=True 不用等視窗。"""
    a = DecryptAttribution(window_s=60.0)
    a.record(111, False, now=0.0, uid=42)
    assert a.summary(now=1.0, force=True).startswith("[異常前基準] (無) ‖ [異常] ")


def test_attrib_sorts_by_fail_desc_and_lists_ok_only():
    """依 fail 由大到小；ok-only 的 ssrc 也列出，fail 寫 0。"""
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
    assert "ok=4(" in seg_333
    assert "fail=0" in seg_333


def test_attrib_features_only_from_failures():
    """fail 子統計的 pt/len 只收失敗封包；ok 封包的 payload 不進 fail。"""
    a = DecryptAttribution(window_s=60.0)
    a.record(111, False, now=0.0, payload=120, size=60)
    a.record(111, False, now=1.0, payload=78, size=200)
    a.record(111, True, now=2.0, payload=99, size=5)
    s = a.summary(now=60.0)
    assert "fail=2(pt=[78, 120] ext=[] cc=[] len=60-200 hdr=-)" in s
    assert "ok=1(pt=[99] ext=[] cc=[] len=5-5 hdr=-)" in s


def test_attrib_max_ssrcs_truncates_with_count():
    """max_ssrcs=2 時 4 個有 fail 的 ssrc → 只列 2 段，尾端 …+2 ssrc。"""
    a = DecryptAttribution(window_s=60.0, max_ssrcs=2)
    for ssrc in (101, 102, 103, 104):
        a.record(ssrc, False, now=0.0)
    s = a.summary(now=60.0)
    assert s.count("ssrc=") == 2
    assert s.endswith("…+2 ssrc")

"""AdaptiveNoiseFloor 單元測試（TDD：先紅後綠）。

單串流版的自適應噪音地板，鏡像 marvin_voice_core/sink.py 的 per-user 演算法：
滾動 75-packet 視窗算平均 RMS，穩定背景（variance<1600）才更新地板；
動態閾值 = max(靜態最低, noise_floor + delta, noise_floor × 1.5)。
供 LocalMicSink 複用——底噪取樣、不寫死單一門檻。
"""
from marvin_voice_core.adaptive_noise_floor import AdaptiveNoiseFloor


def test_initial_threshold_is_static_min_before_learning():
    """尚未學到背景前，門檻由靜態最低值兜底（floor_init 太低不會壓過 static）。"""
    nf = AdaptiveNoiseFloor(static_floor=500)
    thr = nf.update(8000)  # 一發語音，尚未滿視窗
    assert thr == 500
    assert nf.noise_floor == AdaptiveNoiseFloor.FLOOR_INIT


def test_stable_ambient_raises_floor_and_threshold():
    """持續穩定的中等背景（variance<1600）→ 地板抬到背景均值、門檻隨之抬高。"""
    nf = AdaptiveNoiseFloor(static_floor=500)
    thr = 0.0
    for _ in range(AdaptiveNoiseFloor.WINDOW):
        thr = nf.update(600)
    assert nf.noise_floor == 600
    # 門檻 = max(500, 600+100, 600×1.5=900) = 900
    assert thr == 900


def test_learned_floor_rejects_ambient_but_passes_speech():
    """學到 600 背景後：600 的環境音落在門檻下（被當靜音），8000 語音仍過門檻。"""
    nf = AdaptiveNoiseFloor(static_floor=500)
    for _ in range(AdaptiveNoiseFloor.WINDOW):
        nf.update(600)
    assert 600 <= nf.update(600)      # 環境音 rms 不高於門檻 → caller 視為靜音
    assert 8000 > nf.update(8000)     # 真語音遠高於門檻 → speech
    # 對照：固定 500 門檻會把 600 誤判成語音；自適應不會
    assert nf.noise_floor > 500


def test_unstable_input_does_not_update_floor():
    """高變異輸入（忽大忽小）→ 非平穩背景，不更新地板（避免把人聲學成背景）。"""
    nf = AdaptiveNoiseFloor(static_floor=500)
    for i in range(AdaptiveNoiseFloor.WINDOW):
        nf.update(100 if i % 2 == 0 else 5000)  # variance 遠 > 1600
    assert nf.noise_floor == AdaptiveNoiseFloor.FLOOR_INIT


def test_deadlock_recovery_drops_floor_when_input_falls():
    """輸入驟降到地板 40% 以下 → 清空視窗、地板重置（背景變安靜時不卡在舊高地板）。"""
    nf = AdaptiveNoiseFloor(static_floor=500)
    for _ in range(AdaptiveNoiseFloor.WINDOW):
        nf.update(600)
    assert nf.noise_floor == 600
    nf.update(100)  # 100 < 600×0.4=240 → deadlock recovery
    assert nf.noise_floor == 100

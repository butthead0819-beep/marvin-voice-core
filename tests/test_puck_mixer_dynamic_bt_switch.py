"""TDD：device/puck_mixer.py::PuckMixer 運行中動態切換 BT 輸出目標
（2026-08-19）。

背景：原本 BT 目標（BMW/Soundcore 誰優先）只在 volume_server.py 開機那一刻
用 pick_bt_mac() 算一次、寫死進 PuckMixer._device，運行中候選裝置的連線
狀態改變（例如在家用 Soundcore 開機，之後開車出門 BMW 進了範圍）不會自動
換 target，得手動重啟服務。改成 PuckMixer 自己在每次(重)連線時即時重新
挑選（_open_pcm()），並在 _loop() 主迴圈定期主動偵測（_maybe_switch_bt_target()）
——後者處理「兩個候選裝置一度同時在線」這種被動 write-失敗偵測不到的情況。

2026-08-19 第二次修：`_maybe_switch_bt_target()` 內部 pick_bt_mac() 會呼叫
`bluetoothctl` subprocess（可能卡到 5s），原本直接同步擋在 _loop() 播放
主迴圈裡，逼近甚至遠超 periods=16 的 buffer 觸發 XRUN——實機規律性斷播的
真因之一。改成背景 thread 做偵測，_loop() 只讀最近一次算好的結果
（_pending_bt_target），偵測本身不再阻塞音訊迴圈；真的要切換時才呼叫
_open_pcm_with_retry()（那本來就會短暫卡頓，是有感事件，可接受）。測試用
_run_thread_target_inline 讓背景 thread 立刻同步跑完，維持測試決定性。
"""
from unittest.mock import MagicMock, patch

from device.puck_mixer import PuckMixer

BMW = "AA:BB:CC:DD:EE:01"
SOUNDCORE = "AA:BB:CC:DD:EE:02"


class _InlineThread:
    """threading.Thread 的假替身：start() 立刻同步跑 target()，測試不用真的
    等背景 thread、也不用 sleep/join 猜時機。"""

    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        if self._target:
            self._target()


def test_single_candidate_never_triggers_recheck():
    """只有一個候選（改動前的舊介面）——_maybe_switch_bt_target 直接跳過，
    不呼叫 pick_bt_mac，行為跟改動前完全一樣。"""
    mixer = PuckMixer(bt_mac=BMW)
    mixer._last_bt_check = 0.0
    fake_pcm = MagicMock()
    with patch("device.puck_mixer.pick_bt_mac") as mock_pick:
        out = mixer._maybe_switch_bt_target(fake_pcm)
    mock_pick.assert_not_called()
    assert out is fake_pcm


def test_recheck_skipped_before_interval_elapsed():
    mixer = PuckMixer(bt_mac=[BMW, SOUNDCORE])
    mixer._current_mac = SOUNDCORE
    mixer._last_bt_check = __import__("time").time()   # 剛檢查過
    fake_pcm = MagicMock()
    with patch("device.puck_mixer.pick_bt_mac") as mock_pick, \
         patch("device.puck_mixer.threading.Thread", _InlineThread):
        out = mixer._maybe_switch_bt_target(fake_pcm)
    mock_pick.assert_not_called()
    assert out is fake_pcm


def test_recheck_does_not_block_audio_loop_pick_runs_in_background_thread():
    """2026-08-19：偵測本身不能卡住 _loop()——用真的 threading.Thread（不 inline
    替換），確認呼叫當下就回傳，不等 pick_bt_mac 跑完才返回。"""
    mixer = PuckMixer(bt_mac=[BMW, SOUNDCORE])
    mixer._current_mac = SOUNDCORE
    mixer._last_bt_check = 0.0
    fake_pcm = MagicMock()
    started = __import__("threading").Event()
    release = __import__("threading").Event()

    def _slow_pick(candidates):
        started.set()
        release.wait(timeout=2.0)   # 模擬卡住的 bluetoothctl subprocess
        return SOUNDCORE

    with patch("device.puck_mixer.pick_bt_mac", side_effect=_slow_pick):
        out = mixer._maybe_switch_bt_target(fake_pcm)   # 應該立刻回傳，不卡在這裡
    assert out is fake_pcm   # pending 還沒算完，這次不換
    assert started.wait(timeout=1.0)   # 背景 thread 真的有在跑
    release.set()   # 收尾，別留一條卡住的 thread


def test_recheck_after_interval_keeps_pcm_when_target_unchanged():
    mixer = PuckMixer(bt_mac=[BMW, SOUNDCORE])
    mixer._current_mac = SOUNDCORE
    mixer._last_bt_check = 0.0   # 早就過了 BT_RECHECK_INTERVAL_S
    fake_pcm = MagicMock()
    with patch("device.puck_mixer.pick_bt_mac", return_value=SOUNDCORE), \
         patch("device.puck_mixer.threading.Thread", _InlineThread):
        out = mixer._maybe_switch_bt_target(fake_pcm)
    fake_pcm.close.assert_not_called()
    assert out is fake_pcm


def test_recheck_switches_pcm_when_higher_priority_target_becomes_available():
    """開機時鎖定 Soundcore（家用測試），運行中 BMW 進了範圍——優先權更高，
    _maybe_switch_bt_target 該主動斷開重連到 BMW，不用等 write() 失敗。"""
    mixer = PuckMixer(bt_mac=[BMW, SOUNDCORE])
    mixer._current_mac = SOUNDCORE
    mixer._last_bt_check = 0.0
    old_pcm = MagicMock()
    new_pcm = MagicMock()
    with patch("device.puck_mixer.pick_bt_mac", return_value=BMW), \
         patch("device.puck_mixer.threading.Thread", _InlineThread), \
         patch.object(mixer, "_open_pcm_with_retry", return_value=new_pcm) as mock_reopen:
        out = mixer._maybe_switch_bt_target(old_pcm)
    old_pcm.close.assert_called_once()
    mock_reopen.assert_called_once()
    assert out is new_pcm


def test_recheck_survives_stop_during_switch():
    """切換途中被 stop() 喊停（_open_pcm_with_retry 回 None）——回傳舊 pcm，
    不炸例外，_loop() 下一輪自然收尾（跟既有斷線重連的降級邏輯一致）。"""
    mixer = PuckMixer(bt_mac=[BMW, SOUNDCORE])
    mixer._current_mac = SOUNDCORE
    mixer._last_bt_check = 0.0
    old_pcm = MagicMock()
    with patch("device.puck_mixer.pick_bt_mac", return_value=BMW), \
         patch("device.puck_mixer.threading.Thread", _InlineThread), \
         patch.object(mixer, "_open_pcm_with_retry", return_value=None):
        out = mixer._maybe_switch_bt_target(old_pcm)
    assert out is old_pcm


def test_open_pcm_picks_target_dynamically_each_call(monkeypatch):
    """_open_pcm() 每次都重新挑選（不是用建構時就固定的字串）——用兩次不同的
    pick_bt_mac 回傳值模擬候選裝置連線狀態在兩次開場之間換了。"""
    mixer = PuckMixer(bt_mac=[BMW, SOUNDCORE])
    calls = []

    class _FakeAlsaaudio:
        PCM_PLAYBACK = 0
        PCM_NORMAL = 0
        PCM_FORMAT_S16_LE = 0

        @staticmethod
        def PCM(*a, **kw):
            calls.append(kw["device"])
            return MagicMock()

    monkeypatch.setattr("device.puck_mixer.alsaaudio", _FakeAlsaaudio)
    with patch("device.puck_mixer.pick_bt_mac", side_effect=[SOUNDCORE, BMW]):
        mixer._open_pcm()
        mixer._open_pcm()

    assert calls == [f"bluealsa:DEV={SOUNDCORE},PROFILE=a2dp", f"bluealsa:DEV={BMW},PROFILE=a2dp"]
    assert mixer._current_mac == BMW

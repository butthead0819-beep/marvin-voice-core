"""car puck mk2 藍牙裝置優先權選擇：現在 MARVIN_PUCK_BT_MAC 寫死單一 MAC，換裝置
要手動改設定檔+重啟服務。改成「依優先權排序的候選清單，自動挑目前有連線的那個」
（見 project_car_puck_mk2_pi_zero2w_bt_mixer_validated 記憶）。使用者拍板：實務上
BMW 車機跟其他喇叭（如 Soundcore）不會同時連線，但真的兩者都連著時 BMW 優先。"""
from device.volume_server import _parse_connected_macs, pick_bt_mac


# ---- _parse_connected_macs：解析 bluetoothctl 輸出 ----

def test_parse_connected_macs_extracts_mac_addresses():
    out = (
        "Device AA:BB:CC:DD:EE:01 BMW 04900\n"
        "Device AA:BB:CC:DD:EE:02 Soundcore Mini 3 Pro\n"
    )
    assert _parse_connected_macs(out) == {"AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"}


def test_parse_connected_macs_empty_output_returns_empty_set():
    assert _parse_connected_macs("") == set()


def test_parse_connected_macs_ignores_malformed_lines():
    out = "Device\nsome garbage\nDevice AA:BB:CC:DD:EE:03 X\n"
    assert _parse_connected_macs(out) == {"AA:BB:CC:DD:EE:03"}


def test_parse_connected_macs_normalizes_case():
    out = "Device aa:bb:cc:dd:ee:01 BMW\n"
    assert _parse_connected_macs(out) == {"AA:BB:CC:DD:EE:01"}


# ---- pick_bt_mac：candidates 依優先權排序（第一個=BMW，最高優先權） ----

BMW = "AA:BB:CC:DD:EE:01"
SOUNDCORE = "AA:BB:CC:DD:EE:02"


def test_pick_bt_mac_picks_only_connected_candidate():
    assert pick_bt_mac([BMW, SOUNDCORE], connected={SOUNDCORE}) == SOUNDCORE


def test_pick_bt_mac_prefers_higher_priority_when_both_connected():
    assert pick_bt_mac([BMW, SOUNDCORE], connected={BMW, SOUNDCORE}) == BMW


def test_pick_bt_mac_falls_back_to_first_candidate_when_none_connected():
    """查不到連線資訊（bluetoothctl 沒回應/都沒連）時，照樣猜優先權最高的那個去開——
    交給 PuckMixer 既有的 _write_with_reconnect 重試邏輯把連線談起來，不要因為
    偵測失敗就整條不開。"""
    assert pick_bt_mac([BMW, SOUNDCORE], connected=set()) == BMW


def test_pick_bt_mac_single_candidate_behaves_like_old_fixed_mac():
    assert pick_bt_mac([BMW], connected=set()) == BMW
    assert pick_bt_mac([BMW], connected={BMW}) == BMW


def test_pick_bt_mac_no_candidates_returns_none():
    assert pick_bt_mac([], connected=set()) is None


def test_pick_bt_mac_case_insensitive_candidate_match():
    assert pick_bt_mac([BMW.lower(), SOUNDCORE], connected={BMW}) == BMW.lower()

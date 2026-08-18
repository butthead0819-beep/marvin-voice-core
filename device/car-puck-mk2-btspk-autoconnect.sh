#!/bin/bash
# Marvin car puck mk2：讓 Pi Zero 2W 主動保持跟車頭單元(A2DP sink)的連線。
# 邏輯跟 device/btspk-autoconnect.sh（家用 Soundcore 版）一致，只是換了對象的 MAC——
# Pi 是 A2DP 來源(central)，車機是 sink，車機開機不會主動回連，得由 Pi 去 page 它。
#
# 使用前置：先手動配對信任一次，讓車機進 Trusted:yes（否則每次都要重新配對，不會自動連上）：
#   bluetoothctl
#   scan on            # 找到車機的 MAC 後 scan off
#   pair <MAC>
#   trust <MAC>
#
# ⚠️ 2026-08-18 實機踩到：這支腳本無條件每 15s page 車機一次，車不在旁邊時每次
# 都要走完整 page-timeout（藍牙 radio 忙碌一段時間）。Pi Zero 2W 只有一顆藍牙
# 天線/晶片，這段 paging 期間會跟同時在播的 A2DP 音樂搶 hci0 的排程時間，聽感
# 是規律性「斷續+追趕」——家用拿 Soundcore 對照測試（$FALLBACK_MAC）時完整中獎，
# 比 ESP32 架構還慘（ESP32 沒有這種背景連線搶 radio 的問題）。跟
# volume_server.py::pick_bt_mac() 用同一個信號解：每輪重新查 fallback 裝置
# （Soundcore）是不是已經連線——連線中就代表現在是家用測試/車機根本不在附近，
# 這輪跳過 page 車機，只確保 fallback 裝置本身連著就好；fallback 沒連線
# （車機在附近、真的在車上開回這台）才照舊去 page 車機。車機從沒連過的首次開機
# 情境（fallback 也沒連）一樣落到 else 分支繼續 page 車機，跟改動前行為一致，
# 不會卡死。
MAC=__CAR_HEAD_UNIT_MAC__
FALLBACK_MAC=__FALLBACK_MAC__

_should_page_head_unit() {
    if [ -z "$FALLBACK_MAC" ]; then
        return 0   # 沒設 fallback（例如只裝了車機沒接對照喇叭）→ 照舊一律 page
    fi
    if bluetoothctl info "$FALLBACK_MAC" 2>/dev/null | grep -q "Connected: yes"; then
        return 1   # fallback 裝置正連線中＝家用測試，別搶 radio
    fi
    return 0
}

while true; do
    if _should_page_head_unit && ! bluetoothctl info "$MAC" 2>/dev/null | grep -q "Connected: yes"; then
        bluetoothctl connect "$MAC" >/dev/null 2>&1   # 連不到就 page-timeout，下一輪再試
    fi
    sleep 15
done

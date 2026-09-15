#!/bin/bash
# Marvin car puck mk2：WiFi 家用/熱點優先權 + 上車判斷(BMW BT)一併處理。
#
# 背景（2026-09-15 用戶回饋）：Pi Zero 2W 開機時在家連上家用WiFi，離家後家用WiFi
# 訊號沒了，但 NetworkManager 預設不會馬上判斷斷線、重新掃描切去熱點，常常要等
# 很久才有反應。三道防線疊加解同一個問題（非互斥）：
#   1. nmcli connection.autoconnect-priority：iPhone 熱點設得比家用WiFi高——兩個
#      都在範圍內時優先選熱點（不影響「範圍外自動略過」的既有行為）。
#   2. 偵測到已連上 BMW 車機藍牙（跟 car-puck-mk2-btspk-autoconnect.sh 用同一顆
#      MAC/同一套 bluetoothctl 查詢）→ 判定「人在車上」，把家用WiFi 這個
#      connection profile 的 autoconnect 關掉，且如果目前正掛在家用WiFi 上就
#      主動斷開，逼 NM 立刻改連熱點，不用等它自己偵測斷線逾時。
#   3. BMW 藍牙斷線（回到家/下車/BMW 還沒配對上）→ 把家用WiFi profile 的
#      autoconnect 開回來，恢復正常在家連家用WiFi 的行為。
#
# ⚠️ 假設 Pi 跑 Raspberry Pi OS 預設的 NetworkManager（`nmcli`）。若實機是舊式
# dhcpcd + wpa_supplicant，這支腳本不適用，要先確認 `nmcli connection show` 有無輸出。
#
# 使用前置：
#   - 家用WiFi、iPhone 熱點都已經是 nmcli 認得的 connection profile（手動連過一次
#     NM 就會自動建立 profile，`nmcli connection show` 可查名稱）。
#   - 下面變數用實機查出來的值填入；BMW_MAC 跟 car-puck-mk2-btspk-autoconnect.sh
#     的 MAC 是同一顆。
#
# 2026-09-15 實機盤點：`nmcli connection show` 發現家用WiFi同一個 SSID 意外重複
# 掛了兩個 connection profile（一個綁死 interface、一個沒綁——後者應是
# Raspberry Pi Imager 燒錄時自動建立的殘留）。HOME_WIFI_CONS 要列出所有指向
# 家用WiFi的 profile 名稱，不然漏掉的那個還是會被 NM 選中連走；已在實機驗證
# 部署（BMW 連線時兩個 profile 的 autoconnect 都正確被關掉）。
HOME_WIFI_CONS=(__HOME_WIFI_CON_NAME_1__ __HOME_WIFI_CON_NAME_2__)
HOTSPOT_CON=__HOTSPOT_CON_NAME__
BMW_MAC=__CAR_HEAD_UNIT_MAC__

# 一次性：熱點優先權設高於家用WiFi。nmcli modify 是 idempotent，每次開機/重跑都安全。
nmcli connection modify "$HOTSPOT_CON" connection.autoconnect-priority 100 2>/dev/null
for c in "${HOME_WIFI_CONS[@]}"; do
    nmcli connection modify "$c" connection.autoconnect-priority 10 2>/dev/null
done

_bmw_connected() {
    bluetoothctl info "$BMW_MAC" 2>/dev/null | grep -q "Connected: yes"
}

_is_active() {
    nmcli -t -f NAME connection show --active 2>/dev/null | grep -qx "$1"
}

while true; do
    if _bmw_connected; then
        # 人在車上 → 家用WiFi 不該連：關掉 autoconnect，且若正連著就主動斷開
        for c in "${HOME_WIFI_CONS[@]}"; do
            nmcli connection modify "$c" connection.autoconnect no 2>/dev/null
            if _is_active "$c"; then
                nmcli connection down "$c" >/dev/null 2>&1
            fi
        done
    else
        # 沒連 BMW（在家 / 剛開機還沒配對上）→ 恢復家用WiFi 正常自動連線
        for c in "${HOME_WIFI_CONS[@]}"; do
            nmcli connection modify "$c" connection.autoconnect yes 2>/dev/null
        done
    fi
    sleep 15
done

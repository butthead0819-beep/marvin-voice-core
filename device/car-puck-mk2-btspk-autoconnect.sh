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
# 安裝：/usr/local/bin/marvin-car-puck-btspk-autoconnect.sh + systemd/marvin-car-puck-btspk.service
MAC=__CAR_HEAD_UNIT_MAC__
while true; do
    if ! bluetoothctl info "$MAC" 2>/dev/null | grep -q "Connected: yes"; then
        bluetoothctl connect "$MAC" >/dev/null 2>&1   # 連不到就 page-timeout，下一輪再試
    fi
    sleep 15
done

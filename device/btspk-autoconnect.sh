#!/bin/bash
# Marvin device：讓 Pi 主動保持跟 Soundcore Mini(A2DP) 的連線。
# 為什麼要這支：Pi 是 A2DP 來源(central)，喇叭是 sink——喇叭開機不會主動回連，
# 得由 Pi 去 page 它；喇叭關機時 page 不到(page-timeout)故要輪詢重試。
# Soundcore 關機後 bond 會掉(Paired:no/Bonded:no,just-works 配對不留金鑰)，
# 但裝置 Trusted:yes → `bluetoothctl connect` 每次會自動重新配對+連上，毋須手動。
# 安裝：/usr/local/bin/marvin-btspk-autoconnect.sh + systemd/marvin-btspk.service
MAC=3C:39:E7:BA:D9:52
while true; do
    if ! bluetoothctl info "$MAC" 2>/dev/null | grep -q "Connected: yes"; then
        bluetoothctl connect "$MAC" >/dev/null 2>&1   # 連不到就 page-timeout，下一輪再試
    fi
    sleep 15
done

#!/bin/bash
# Marvin car puck mk2：開機自動播放——比照 ESP32 firmware carHeartbeat() 的做法
# （見 project_car_puck_boot_autoplay_streaming 記憶）：POST /car {"state":"present"}
# 給 Mac 端，第一次觸發 CarPresence.on_arrive()（讀時段建開場、透過 inject_text
# 「放一首X」走正常點歌管線，pi_bt 硬體下會經 _get_puck_client() 推給這台 Pi 的
# /puck/play），之後每 30s 續一次心跳保活；斷電＝這支腳本跟著斷電停送，Mac 端
# CarPresence 90s TTL 收尾自動停播，Pi 不用主動送 absent。
#
# 這支是 Pi→Mac 方向，跟 device/puck_mixer.py 的 /puck/* 播放端點（Mac→Pi）完全
# 獨立、方向相反：這支只負責「叫醒 Mac 開始決策」，實際放音樂還是 Mac 決定歌單後
# 呼叫 Pi 的 /puck/play 才會真的出聲。
#
# 前置：Mac 的 .env 要有 MARVIN_CAR_MODE=1（已預設開）+ MARVIN_CAR_HARDWARE=pi_bt
# + MARVIN_PUCK_BASE_URL=http://<這台Pi的Tailscale IP>:8766 + MARVIN_PUCK_TOKEN
# （= Mac .env 的 MARVIN_TEXT_TOKEN，跟下面 TOKEN 用同一個值）。
#
# 安裝：/usr/local/bin/marvin-car-puck-presence-heartbeat.sh + systemd/marvin-car-puck-presence.service
MAC_HOST=__MAC_SATELLITE_HOST__   # 例：100.123.68.86:8790（Mac Tailscale IP，見 device/systemd/marvin-hud-kiosk.service 同款用法）
TOKEN=__MARVIN_TEXT_TOKEN__

while true; do
    curl -sf -m 5 -X POST "http://${MAC_HOST}/car" \
        -H "Content-Type: application/json" \
        -H "X-Marvin-Token: ${TOKEN}" \
        -d '{"state":"present"}' >/dev/null 2>&1
    sleep 30
done

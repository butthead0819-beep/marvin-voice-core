#!/bin/bash
# temp_guard.sh — Marvin device SoC 溫度防護。
# SoC 溫度超過門檻 → POST「停止播放」給 Mac 大腦，避免擴大機持續過熱把 24V 拉垮重開。
#
# ⚠️ 重要限制：Pi 只讀得到 SoC(cpu-thermal) 溫度，讀不到 DigiAMP+ 的 TAS5756M 擴大機晶片
#    溫度——而過熱的其實是那顆晶片。SoC 溫度只是「弱代理」（同一疊板有熱耦合會跟著升，
#    但幅度小、會遲滯）。所以這是「安全網」不是根治；根治＝給 TAS5756M 貼散熱片。
#    門檻請視實測調整（env MARVIN_TEMP_STOP_C / MARVIN_TEMP_CLEAR_C）。
#
# 遲滯：升到 STOP_C 觸發停播並進入警戒；降到 CLEAR_C 以下才解除，避免在門檻附近抖動狂送。

MAC_SAY="${MARVIN_MAC_SAY_URL:-http://100.123.68.86:8790/say}"
TOKEN="${MARVIN_TEXT_TOKEN:-}"
STOP_C="${MARVIN_TEMP_STOP_C:-65}"     # SoC ≥ 此溫度(°C) → 停播
CLEAR_C="${MARVIN_TEMP_CLEAR_C:-57}"   # SoC < 此溫度(°C) → 解除警戒
POLL="${MARVIN_TEMP_POLL:-5}"          # 取樣秒數
LOG=/var/log/temp_guard.log

echo "$(date +%F_%T) temp_guard 啟動 stop=${STOP_C} clear=${CLEAR_C} poll=${POLL}s" >> "$LOG"; sync
armed=1
while true; do
  t=$(vcgencmd measure_temp 2>/dev/null | grep -oE '[0-9]+\.[0-9]+')
  ti=${t%.*}
  if [ -n "$ti" ]; then
    if [ "$armed" = "1" ] && [ "$ti" -ge "$STOP_C" ]; then
      echo "$(date +%F_%T) 🔥 SoC ${t}°C ≥ ${STOP_C} → 送停止播放" >> "$LOG"; sync
      curl -s -m 8 -X POST "${MAC_SAY}?t=${TOKEN}" --data "停止播放" >/dev/null 2>&1
      armed=0
    elif [ "$armed" = "0" ] && [ "$ti" -lt "$CLEAR_C" ]; then
      echo "$(date +%F_%T) ✅ SoC ${t}°C < ${CLEAR_C} → 解除警戒" >> "$LOG"; sync
      armed=1
    fi
  fi
  sleep "$POLL"
done

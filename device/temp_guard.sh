#!/bin/bash
# temp_guard.sh — Marvin device SoC 溫度防護。
# SoC 溫度超過門檻 → 本機靜音 DigiAMP（amixer Digital mute），停掉擴大機驅動喇叭的發熱源，
# 趕在 TAS5756M 過熱把 24V/3A 拉垮、連帶 Pi 5V 垮掉斷電重開「之前」搶救。
#
# ⚠️ 重要限制：Pi 只讀得到 SoC(cpu-thermal) 溫度，讀不到 DigiAMP+ 的 TAS5756M 擴大機晶片
#    溫度——真正過熱的是那顆晶片。SoC 只是「弱代理」：晶片的熱透過同疊板耦合，讓 SoC 微幅、
#    遲滯地跟著升。實測 idle≈55°C（Pi CPU 自身底噪）、有播放時才會爬過門檻。所以這是
#    「趕在硬斷前搶救的安全網」不是根治；根治＝給 TAS5756M 貼散熱片 ＋ 24V/5A 供電。
#
# 靜音是「黏著」的、不自動解除：自動解除會讓喇叭重新被驅動→再度發熱→可能又斷電。
#    冷卻後手動解除：`marvin-mic amp on`（或控制台 🏠 按鈕）。
#
# 遲滯：升到 STOP_C 觸發靜音並解除警戒；降到 < CLEAR_C 才重新武裝（可再次觸發）。
# ⚠️ STOP=50 取較大安全裕度、及早在遲滯的 SoC 代理爬高前砍發熱。離家 idle 時 amp 本來就被
#    marvin-mic off 靜音（再靜音一次無害），故 idle 觸發不影響離家；在家待機(amp on)idle≈55
#    會被觸發靜音，播歌前需先 `marvin-mic amp on`。CLEAR=48＝離家靜音冷卻後重新武裝的門檻，
#    若實測離家靜音 idle 仍高於 48 而不再武裝，往下調。可用 env 覆寫。

AMP_CARD="${MARVIN_AMP_CARD:-IQaudIODAC}"
AMP_CTL="${MARVIN_AMP_CTL:-Digital}"
STOP_C="${MARVIN_TEMP_STOP_C:-50}"     # SoC ≥ 此溫度(°C) → 靜音
CLEAR_C="${MARVIN_TEMP_CLEAR_C:-48}"   # SoC < 此溫度(°C) → 重新武裝
POLL="${MARVIN_TEMP_POLL:-5}"          # 取樣秒數
LOG=/var/log/temp_guard.log

echo "$(date +%F_%T) temp_guard 啟動 stop=${STOP_C} clear=${CLEAR_C} poll=${POLL}s action=mute(${AMP_CARD}/${AMP_CTL})" >> "$LOG"; sync
armed=1
while true; do
  t=$(vcgencmd measure_temp 2>/dev/null | grep -oE '[0-9]+\.[0-9]+')
  ti=${t%.*}
  if [ -n "$ti" ]; then
    if [ "$armed" = "1" ] && [ "$ti" -ge "$STOP_C" ]; then
      echo "$(date +%F_%T) 🔥 SoC ${t}°C ≥ ${STOP_C} → 靜音 DigiAMP(${AMP_CTL})" >> "$LOG"; sync
      amixer -c "$AMP_CARD" sset "$AMP_CTL" mute >/dev/null 2>&1
      armed=0
    elif [ "$armed" = "0" ] && [ "$ti" -lt "$CLEAR_C" ]; then
      echo "$(date +%F_%T) ✅ SoC ${t}°C < ${CLEAR_C} → 重新武裝（仍保持靜音，需 marvin-mic amp on 手動解除）" >> "$LOG"; sync
      armed=1
    fi
  fi
  sleep "$POLL"
done

#!/bin/bash
LOG=/var/log/voltmon.log
echo "$(date +%F_%T) voltmon 啟動 $(vcgencmd measure_volts)" >> $LOG; sync
while true; do
  T=$(vcgencmd get_throttled)
  if [ "$T" != "throttled=0x0" ]; then
    echo "$(date +%F_%T) $T $(vcgencmd measure_volts) load=$(cat /proc/loadavg|cut -d\  -f1)" >> $LOG
    sync
  fi
  sleep 2
done

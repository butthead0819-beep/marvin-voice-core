#!/bin/bash
# marvinctl — Mac 端「腦」開關：看現況 / 原子切換 Discord ⇄ device。
#
# 為什麼需要它：Discord bot(main_discord.py, launchd) 與 device 腦(main_satellite.py)
# 共用同一份記憶(marvin.db/music_memory.json/records)、無跨進程鎖。兩者同時跑＝兩個腦
# 寫同一份記憶→lost-update/記憶損毀。互斥靠「停一啟一」紀律撐著，此工具把紀律變成機制：
# 每個切換指令都先確認另一具真的死了、才啟動目標→從機制上杜絕雙腦。
# Pi(身體)是 :10700 上待命的啞 I/O，開機自動起、不受這裡影響。
#
# 用法：
#   marvinctl status     看兩具狀態 + 當前活著的腦
#   marvinctl device     切到 device：停 Discord → 起 main_satellite（背景+log）
#   marvinctl discord     切到 Discord：停 main_satellite → 起 Discord launchd bot
#   marvinctl toggle     翻到另一個模式（Discord↔device；都沒跑→device）＝給 Hey Siri 一句話用
#   marvinctl logs [n]   tail 當前活著那具的 log（預設 40 行）
# 註：不用 set -e——本工具大量呼叫「預期會非零」的指令(pgrep 無匹配、bootout 未載入)，
# 互斥安全改用顯式 stop_x || exit 1 把關（stop 沒成功就不啟動另一具）。
set -uo pipefail

# ── 設定（可用環境變數覆蓋）──────────────────────────────────────────────────
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.antigravity.marvin.bot"
DOMAIN="gui/$(id -u)"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
PY="$REPO/venv_simon/bin/python"
DISCORD_LOG="$HOME/Library/Logs/Marvin/bot_stdout.log"
SAT_LOG="$HOME/Library/Logs/Marvin/satellite.log"
SAT_SPEAKER="${MARVIN_SATELLITE_SPEAKER:-狗與露}"
SAT_HOST="${MARVIN_SATELLITE_HOST:-marvinpi.local}"

# ── 顏色 ─────────────────────────────────────────────────────────────────────
if [ -t 1 ]; then G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; B=$'\033[1m'; Z=$'\033[0m'; else G=; R=; Y=; B=; Z=; fi

# 用 basename 比對：device 腦以 `cd repo && python main_satellite.py` 啟動，argv 是相對路徑，
# 比對完整路徑會漏。cmdline 含 main_*.py 已足夠唯一（wrapper run_bot.py 不含此字串）。
discord_pid() { pgrep -f "main_discord.py" 2>/dev/null | head -1 || true; }
device_pid()  { pgrep -f "main_satellite.py" 2>/dev/null | head -1 || true; }
launchd_loaded() { launchctl print "${DOMAIN}/${LABEL}" >/dev/null 2>&1; }

# 等某個 pidfn 回空（死透）；逾時回 1
wait_gone() {
    local fn="$1" name="$2" i
    for i in $(seq 1 40); do
        [ -z "$($fn)" ] && return 0
        sleep 0.5
    done
    echo "${R}✗ ${name} 15s 內沒停掉，為保護記憶不啟動另一具。手動查：pgrep -fl ${name}${Z}" >&2
    return 1
}

cmd_status() {
    local dpid spid
    dpid="$(discord_pid)"; spid="$(device_pid)"
    echo "${B}Marvin 腦（Mac 端，一次只該一具）${Z}"
    if [ -n "$dpid" ]; then echo "  Discord bot   ${G}● 運行中${Z} (pid $dpid, launchd)"
    else echo "  Discord bot   ${Y}○ 停止${Z}$(launchd_loaded && echo '（launchd 已載入但無進程）')"; fi
    if [ -n "$spid" ]; then echo "  device 腦     ${G}● 運行中${Z} (pid $spid → Pi ${SAT_HOST}, 講者=${SAT_SPEAKER})"
    else echo "  device 腦     ${Y}○ 停止${Z}"; fi
    echo -n "  ${B}→ 當前身體：${Z}"
    if [ -n "$dpid" ] && [ -n "$spid" ]; then echo "${R}⚠️ 兩具都在跑！記憶有損毀風險，快 marvinctl device 或 discord 收斂${Z}"
    elif [ -n "$dpid" ]; then echo "${G}Discord${Z}"
    elif [ -n "$spid" ]; then echo "${G}device（實體音箱）${Z}"
    else echo "${Y}都沒跑${Z}"; fi
    # Pi 身體狀態（順帶）
    echo -n "  Pi 身體(${SAT_HOST}:10700)："
    if nc -z -G 2 "$SAT_HOST" 10700 2>/dev/null; then echo "${G}● 待命${Z}"; else echo "${Y}○ 不可達（沒開機/沒連網）${Z}"; fi
}

# 進度訊息走 stderr：Shortcuts 只抓 stdout→Siri 只唸最後一句結果；互動終端 stderr 照樣可見。
note() { echo "$@" >&2; }

stop_discord() {
    if launchd_loaded || [ -n "$(discord_pid)" ]; then
        note "  停 Discord bot…"
        launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
        wait_gone discord_pid main_discord.py || return 1
    fi
    return 0
}

start_discord() {
    note "  起 Discord bot…"
    if launchd_loaded; then launchctl kickstart -k "${DOMAIN}/${LABEL}"
    else launchctl bootstrap "$DOMAIN" "$PLIST"; fi
}

stop_device() {
    if [ -n "$(device_pid)" ]; then
        note "  停 device 腦…"
        pkill -TERM -f "main_satellite.py" 2>/dev/null || true
        wait_gone device_pid main_satellite.py || return 1
    fi
    return 0
}

start_device() {
    note "  起 device 腦（連 Pi ${SAT_HOST}, 講者=${SAT_SPEAKER}）…"
    mkdir -p "$(dirname "$SAT_LOG")"
    ( cd "$REPO" && MARVIN_SATELLITE_SPEAKER="$SAT_SPEAKER" MARVIN_SATELLITE_HOST="$SAT_HOST" \
        nohup "$PY" main_satellite.py >>"$SAT_LOG" 2>&1 & )
}

# 當前身體的口語名（給 Siri 唸）
current_body() {
    if [ -n "$(device_pid)" ]; then echo "音箱"
    elif [ -n "$(discord_pid)" ]; then echo "Discord"
    else echo "沒有任何模式"; fi
}
# 切換後回報：互動終端→完整 status；非互動(Siri/Shortcuts)→一句話好唸
report_switch() {
    if [ -t 1 ]; then echo; cmd_status
    else echo "馬文已切換到 $(current_body) 模式"; fi
}

cmd_device() {
    note "${B}切到 device 模式${Z}"
    stop_discord || exit 1             # 先確認 Discord 腦死透（互斥）；沒停成就不啟動
    if [ -n "$(device_pid)" ]; then note "  device 腦已在跑，不重啟。"; else start_device; sleep 2; fi
    report_switch
}

cmd_discord() {
    note "${B}切到 Discord 模式${Z}"
    stop_device || exit 1              # 先確認 device 腦死透（互斥）；沒停成就不啟動
    if [ -n "$(discord_pid)" ]; then note "  Discord bot 已在跑，不重啟。"; else start_discord; sleep 2; fi
    report_switch
}

cmd_toggle() {
    # 翻到「另一個」腦：Discord 在跑→去 device；device 在跑→去 Discord；都沒跑→開 device。
    if [ -n "$(discord_pid)" ]; then cmd_device
    elif [ -n "$(device_pid)" ]; then cmd_discord
    else cmd_device; fi
}

cmd_logs() {
    local dpid spid n="${1:-40}"
    dpid="$(discord_pid)"; spid="$(device_pid)"
    if [ -n "$spid" ]; then echo "${B}== device log ($SAT_LOG) ==${Z}"; tail -n "$n" "$SAT_LOG"
    elif [ -n "$dpid" ]; then echo "${B}== Discord log ($DISCORD_LOG) ==${Z}"; tail -n "$n" "$DISCORD_LOG"
    else echo "${Y}兩具都沒跑，無 log 可看。${Z}"; fi
}

case "${1:-status}" in
    status|"") cmd_status ;;
    device)    cmd_device ;;
    discord)   cmd_discord ;;
    toggle)    cmd_toggle ;;
    logs)      cmd_logs "${2:-40}" ;;
    *) echo "用法: marvinctl {status|device|discord|toggle|logs [n]}" >&2; exit 1 ;;
esac

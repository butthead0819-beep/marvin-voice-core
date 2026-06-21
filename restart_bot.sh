#!/bin/bash
# Bot 重啟腳本
cd "$(dirname "$0")"

# 停止舊的 bot 進程
# 注意：bot 是多進程（1 主 + N 子），pgrep 會回多個 PID。
# 早期版本用 kill "$OLD_PID"（加引號）→ 多 PID 變單一參數、kill 報錯、舊 bot 沒死，
# 新 bot 啟動時撞 port 8765/8766/8767 → Marmo/Companion/GameWSHub 綁不上（降級啟動），
# 甚至雙 bot 搶麥。改用 pkill 反覆殺到乾淨，並等 port 釋放。
if pgrep -f "python.*main_discord.py" >/dev/null 2>&1; then
    echo "🛑 停止舊 bot..."
    for i in 1 2 3 4 5; do
        pkill -f "python.*main_discord.py" 2>/dev/null
        sleep 2
        REMAIN=$(pgrep -f "python.*main_discord.py" | wc -l | tr -d ' ')
        [ "$REMAIN" = "0" ] && break
        echo "   仍有 $REMAIN 個進程，第 $i 次升級 SIGKILL..."
        pkill -9 -f "python.*main_discord.py" 2>/dev/null
    done
    # 等三個 aux port 釋放，避免新 bot 降級啟動
    for i in 1 2 3 4 5; do
        if lsof -nP -iTCP:8765 -iTCP:8766 -iTCP:8767 -sTCP:LISTEN >/dev/null 2>&1; then
            sleep 1
        else
            break
        fi
    done
fi

# 啟動新的 bot（背景執行；Python 內部會將 stdout/stderr 寫入可輪替的 bot_stdout.log）
echo "🚀 啟動 bot..."
source venv_simon/bin/activate
nohup python main_discord.py > bot_bootstrap.log 2>&1 &

echo "✅ Bot 已啟動 (PID: $!)"
echo "📋 查看 log: tail -f bot_stdout.log"

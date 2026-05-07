#!/bin/bash
# Bot 重啟腳本
cd "$(dirname "$0")"

# 停止舊的 bot 進程
OLD_PID=$(pgrep -f "python.*main_discord.py" 2>/dev/null)
if [ -n "$OLD_PID" ]; then
    echo "🛑 停止舊 bot (PID: $OLD_PID)..."
    kill "$OLD_PID"
    sleep 2
fi

# 啟動新的 bot（背景執行；Python 內部會將 stdout/stderr 寫入可輪替的 bot_stdout.log）
echo "🚀 啟動 bot..."
source venv_simon/bin/activate
nohup python main_discord.py > bot_bootstrap.log 2>&1 &

echo "✅ Bot 已啟動 (PID: $!)"
echo "📋 查看 log: tail -f bot_stdout.log"

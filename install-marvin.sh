#!/usr/bin/env bash
# install-marvin.sh — 5-minute Marvin setup for macOS streamers.
# Usage: curl -fsSL https://raw.githubusercontent.com/butthead0819-beep/marvin-voice-core/main/install-marvin.sh | bash

set -euo pipefail

readonly INSTALL_DIR="${HOME}/marvin"
readonly REPO_URL="https://github.com/butthead0819-beep/marvin-voice-core.git"
readonly REQUIRED_PY="3.12"

# ── Colored output ───────────────────────────────────────────────────────────
if [ -t 1 ]; then
    readonly C_GREEN=$'\033[0;32m'
    readonly C_YELLOW=$'\033[0;33m'
    readonly C_RED=$'\033[0;31m'
    readonly C_BLUE=$'\033[0;34m'
    readonly C_BOLD=$'\033[1m'
    readonly C_RESET=$'\033[0m'
else
    readonly C_GREEN='' C_YELLOW='' C_RED='' C_BLUE='' C_BOLD='' C_RESET=''
fi

say()  { printf "%s%s%s\n" "$C_BLUE" "$1" "$C_RESET"; }
ok()   { printf "%s✓ %s%s\n" "$C_GREEN" "$1" "$C_RESET"; }
warn() { printf "%s⚠ %s%s\n" "$C_YELLOW" "$1" "$C_RESET"; }
die()  { printf "%s✗ %s%s\n" "$C_RED" "$1" "$C_RESET" >&2; exit 1; }

# ── Sanity checks ────────────────────────────────────────────────────────────
[ "$(uname -s)" = "Darwin" ] || die "Marvin 只支援 macOS，這台是 $(uname -s)"

say ""
say "${C_BOLD}🤖 Marvin 安裝器 v1${C_RESET}"
say "預估時間：5-10 分鐘（首次裝 Homebrew 會比較久）"
say ""

# ── Step 1: Homebrew ─────────────────────────────────────────────────────────
say "[1/5] 檢查 Homebrew..."
if command -v brew >/dev/null 2>&1; then
    ok "Homebrew 已安裝"
else
    warn "Homebrew 沒裝，現在安裝（5-10 分鐘）"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Add brew to PATH for Apple Silicon Macs
    if [ -d /opt/homebrew/bin ]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    fi
    ok "Homebrew 裝好"
fi

# ── Step 2: Python 3.12 + git ────────────────────────────────────────────────
say ""
say "[2/5] 檢查 Python 3.12 與 git..."
if ! command -v "python${REQUIRED_PY}" >/dev/null 2>&1; then
    warn "Python ${REQUIRED_PY} 沒裝，裝中..."
    brew install "python@${REQUIRED_PY}"
fi
if ! command -v git >/dev/null 2>&1; then
    warn "git 沒裝，裝中..."
    brew install git
fi
ok "Python $(python${REQUIRED_PY} --version | cut -d' ' -f2) + $(git --version | cut -d' ' -f3) 就緒"

# ── Step 3: Clone / update repo ──────────────────────────────────────────────
say ""
say "[3/5] 下載 Marvin 程式碼..."
if [ -d "$INSTALL_DIR/.git" ]; then
    warn "$INSTALL_DIR 已存在，拉最新版"
    cd "$INSTALL_DIR" && git pull --ff-only || warn "git pull 失敗，跳過更新（用既有版本）"
else
    git clone --depth=1 "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi
ok "Marvin 程式碼在 $INSTALL_DIR"

# ── Step 4: Python deps ──────────────────────────────────────────────────────
say ""
say "[4/5] 安裝 Python 套件（3-5 分鐘）..."
"python${REQUIRED_PY}" -m pip install --upgrade pip --quiet
"python${REQUIRED_PY}" -m pip install -r requirements.txt --quiet
ok "套件安裝完成"

# ── Step 5: .env wizard ──────────────────────────────────────────────────────
say ""
say "[5/5] 設定 API keys"
say "等下你會被問 3 個 key，現在準備好："
say "  1. Discord Bot Token — https://discord.com/developers/applications"
say "  2. Groq API Key       — https://console.groq.com/keys"
say "  3. Gemini API Key     — https://aistudio.google.com/apikey"
say ""
say "（如果還沒申請，先去申請好再回來繼續。按 Ctrl+C 中止，跑完再 re-run 這個指令繼續）"
say ""
read -r -p "按 Enter 繼續..." _

# Detect if running via pipe (stdin not a tty) and prompt from /dev/tty
read_secret() {
    local prompt="$1"
    local var
    # -s 隱藏輸入避免 token 流進 terminal scrollback / iTerm log
    if [ -t 0 ]; then
        read -rs -p "$prompt" var
    else
        read -rs -p "$prompt" var </dev/tty
    fi
    echo "" >&2  # 換行（-s 不會自動換行）
    echo "$var"
}

validate_key() {
    local name="$1"
    local val="$2"
    local min_len="${3:-20}"
    if [ -z "$val" ]; then
        die "$name 是空的。請重新跑這個指令，貼上有效的 key。"
    fi
    if [ "${#val}" -lt "$min_len" ]; then
        die "$name 看起來太短（${#val} 字元，預期至少 $min_len）。可能複製貼上時截斷了。"
    fi
}

if [ -f .env ]; then
    warn ".env 已存在，備份為 .env.backup-$(date +%s) 後重新生成"
    cp .env ".env.backup-$(date +%s)"
fi

# Start from .env.example
cp .env.example .env

say ""
DISCORD_TOKEN=$(read_secret "貼上 Discord Bot Token (輸入不會顯示): ")
validate_key "Discord Bot Token" "$DISCORD_TOKEN" 50
GROQ_KEY=$(read_secret "貼上 Groq API Key (輸入不會顯示): ")
validate_key "Groq API Key" "$GROQ_KEY" 30
GEMINI_KEY=$(read_secret "貼上 Gemini API Key (輸入不會顯示): ")
validate_key "Gemini API Key" "$GEMINI_KEY" 30

# Write to .env (use temp file then mv for atomicity)
TMP=$(mktemp)
while IFS= read -r line; do
    case "$line" in
        "DISCORD_BOT_TOKEN="*)      echo "DISCORD_BOT_TOKEN=$DISCORD_TOKEN" >> "$TMP" ;;
        "GROQ_API_KEY="*)           echo "GROQ_API_KEY=$GROQ_KEY" >> "$TMP" ;;
        "GEMINI_API_KEY="*)         echo "GEMINI_API_KEY=$GEMINI_KEY" >> "$TMP" ;;
        "GOOGLE_API_KEY="*)         echo "GOOGLE_API_KEY=$GEMINI_KEY" >> "$TMP" ;;
        "GEMINI_CLEANER_API_KEY="*) echo "GEMINI_CLEANER_API_KEY=$GEMINI_KEY" >> "$TMP" ;;
        *)                          echo "$line" >> "$TMP" ;;
    esac
done < .env
mv "$TMP" .env
chmod 600 .env

# 驗證所有 3 個 key 都成功寫進 .env（防止 .env.example 缺欄位導致空 key 滲漏）
for required in DISCORD_BOT_TOKEN GROQ_API_KEY GEMINI_API_KEY; do
    if ! grep -qE "^${required}=.+$" .env; then
        die "$required 沒寫進 .env（.env.example 可能缺這個欄位）。檢查 .env.example 或回報維護者。"
    fi
done
ok ".env 寫好（權限 600，只有你能讀；3 個 key 都驗證過）"

# ── Done ─────────────────────────────────────────────────────────────────────
say ""
say "${C_BOLD}🎉 安裝完成！${C_RESET}"
say ""
say "${C_BOLD}下一步：${C_RESET}"
say ""
say "1. 回 Discord Developer Portal 確認 Bot 的 ${C_BOLD}三個 Intents 全打開${C_RESET}："
say "   • PRESENCE INTENT"
say "   • SERVER MEMBERS INTENT"
say "   • MESSAGE CONTENT INTENT"
say ""
say "2. 用 OAuth2 URL Generator 產 invite URL → 邀請 bot 進你的 server"
say "   （Scopes: bot + applications.commands；"
say "    Permissions: Send Messages + Connect + Speak + Use Voice Activity + Read Message History）"
say ""
say "3. 啟動 Marvin："
say "   ${C_BOLD}cd ${INSTALL_DIR} && python${REQUIRED_PY} main_discord.py${C_RESET}"
say ""
say "4. Discord 進語音頻道後打 ${C_BOLD}/summon${C_RESET}"
say ""
say "卡住請看 ${C_BOLD}docs/STREAMER_SETUP.md${C_RESET} 的 troubleshooting 區，或 DM 維護者求救。"

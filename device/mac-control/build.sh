#!/usr/bin/env bash
# 把 MarvinControl.swift 編成常駐選單列 .app（無 Dock 圖示）。不必開 Xcode。
# 需求：Xcode command line tools（swiftc）、macOS 13+。
set -euo pipefail
cd "$(dirname "$0")"

APP="build/MarvinControl.app"
BIN="$APP/Contents/MacOS/MarvinControl"

rm -rf build
mkdir -p "$APP/Contents/MacOS"

echo "→ 編譯 MarvinControl.swift"
swiftc -O -parse-as-library MarvinControl.swift -o "$BIN"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleExecutable</key><string>MarvinControl</string>
  <key>CFBundleIdentifier</key><string>com.marvin.control</string>
  <key>CFBundleName</key><string>MarvinControl</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
</dict></plist>
PLIST

echo "✓ 完成：$APP"
echo "  執行：open $APP"
echo "  開機自動啟動：把 $APP 拉進 系統設定 → 一般 → 登入項目"

#!/usr/bin/env bash
# 一鍵啟用本機備援執行器。
# 之後每天 Taipei 21:30 / 00:15 / 03:50 自動跑 main_bot.py --execute。
# 停用：bash uninstall.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PLIST_DST="$HOME/Library/LaunchAgents/com.tradebot.runner.plist"

# 1. 模板 placeholder 替換 → 寫入 LaunchAgents
sed "s|__INSTALL_PATH__|$SCRIPT_DIR|g" local_runner.plist > "$PLIST_DST"
chmod 600 "$PLIST_DST"

# 2. 確保 runner.sh 可執行
chmod +x local_runner.sh

# 3. 載入（若已載入先卸載重來，等同重啟）
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load -w "$PLIST_DST"

echo "✓ 本機備援已啟用"
echo ""
echo "排程（Taipei 時間）："
echo "  21:54 — 開盤掃（GH 兩次都漏才上場）"
echo "  00:37 — 盤中掃（GH 兩次都漏才上場）"
echo "  03:55 — 收盤前掃（夾在 GH primary/backup 中間，避開市場關盤）"
echo ""
echo "Log：tail -f /tmp/trade_bot_runner.log"
echo "停用：bash $SCRIPT_DIR/uninstall.sh"
echo ""
echo "提示：第一次跑時 macOS 可能會跳「允許通知」視窗，請允許。"

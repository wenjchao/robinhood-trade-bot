#!/usr/bin/env bash
# 一鍵停用本機備援執行器。

set -uo pipefail

PLIST_DST="$HOME/Library/LaunchAgents/com.tradebot.runner.plist"

if [ -f "$PLIST_DST" ]; then
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    rm -f "$PLIST_DST"
    echo "✓ 本機備援已停用"
else
    echo "（本來就沒在跑）"
fi

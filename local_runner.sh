#!/usr/bin/env bash
# 本機備援執行器 — GitHub Actions cron 的本機備援。
#
# 由 launchd（~/Library/LaunchAgents/com.tradebot.runner.plist）每天三次觸發。
# 跑 main_bot.py --execute、把結果 post 回 GitHub Issue #1（[local] 標籤）。
#
# 為什麼可以兩邊都跑：bot 本身 idempotent
#   - check_open_orders 防止「同時段內」雙重下單
#   - decide() 看當下狀態，已平衡時 → action: hold
# 流程：
#   - local 跑在 GH primary 前 ~3 分鐘 → 若 Mac 開著就先做完
#   - GH 隨後跑 → 看到狀態已平衡 → hold
#   - Mac 關著 / local 沒跑 → GH 正常擔綱
#   - GH 也漏跑 → 漏這時段，下一時段重算（bot 自帶這個邏輯）

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

LOG="/tmp/trade_bot_runner.log"
exec >> "$LOG" 2>&1

echo ""
echo "=== $(date -u +"%Y-%m-%d %H:%M:%S UTC") (local) ==="

# 跑 bot，stdout 也寫到 BOT_OUT 變數做後續解析
BOT_LOG=$(mktemp)
trap "rm -f $BOT_LOG" EXIT

if ! uv run main_bot.py --execute > "$BOT_LOG" 2>&1; then
    JOB_STATUS="failure"
else
    JOB_STATUS="success"
fi
cat "$BOT_LOG"

# 解析 action / placed（跟 workflow yaml 同樣邏輯）
ACTION="unknown"
if grep -q "Skipping this cycle" "$BOT_LOG"; then
    ACTION="skipped-open-orders"
else
    VAL=$(grep "^Action:" "$BOT_LOG" | head -1 | awk '{print $2}')
    [ -n "$VAL" ] && ACTION="$VAL"
fi
PLACED=$(grep -c ">>> PLACED" "$BOT_LOG" || true)

# 撈 repo slug（從 git remote 解出 owner/name）
# 用兩個簡單 sed 而非一個 regex，避開 macOS BSD sed 對 (...)? 的相容問題
REPO=$(git config --get remote.origin.url \
       | sed -E 's|.*github\.com[/:]||; s|\.git$||')

NOW_UTC=$(date -u +"%Y-%m-%d %H:%M UTC")
NOW_ET=$(TZ=America/New_York date +"%H:%M ET")

# Post 到 Issue #1（[local] 標籤）
gh issue comment 1 --repo "$REPO" --body \
"**[local] $NOW_UTC** ($NOW_ET) • run: \`$JOB_STATUS\` • action: \`$ACTION\` • placed: \`$PLACED\` • trigger: \`launchd\`" \
    || echo "(issue comment failed, but bot ran)"

# 通知：只有「本機真的做事」才嗶——用來提示「GH 漏跑了，本機接手」
if [ "$PLACED" -gt 0 ] || [ "$JOB_STATUS" = "failure" ]; then
    osascript -e "display notification \"action=$ACTION placed=$PLACED\" with title \"trade_bot 本機執行\"" || true
fi

exit 0

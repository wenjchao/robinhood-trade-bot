"""本機 GH Actions 排程監看器。

設計目的：GH Actions cron 有時會整天 skip 不跑（已實際遇到）。這支腳本在
你 Mac 上跑，定時檢查 GitHub Issue #1 的 comment 是否符合昨天交易日的
預期排程，缺一個時段就跳 macOS 原生通知。

跑頻：透過 launchd 設定每天 07:00 Taipei 跑一次（local_monitor.plist）。
電腦關著的早上就略過那次檢查——不會誤報，也不需要 24/7 開機。

執行：
    uv run local_monitor.py            # 跑一次檢查（launchd 會自動叫）
    uv run local_monitor.py --test     # 強制送一個通知測試 macOS 權限

不會碰 Robinhood、不會下單，只讀 GitHub Issue。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Issue #1 是「Bot status」issue。main_bot.py 跑完每次會 append 一則 comment
ISSUE_NUMBER = 1

# 每個時段在 UTC 的「主」排程時間（備援在 8 分鐘後，給整體 60 分鐘的容許窗）
# 跟 .github/workflows/rebalance.yml 的 cron 設定保持同步
SCHEDULE_SLOTS_UTC = [
    ("open",  13, 34),   # 9:34 ET
    ("mid",   16, 17),   # 12:17 ET
    ("close", 19, 53),   # 15:53 ET
]

# 容許 GH cron 延遲 / 備援的窗口長度（分鐘）
# 主排程後這段時間內如有任何 comment 落入，視為「這個時段有跑」
GRACE_MINUTES = 60


def get_repo_slug() -> str:
    """從 git remote.origin.url 解析 owner/repo（這樣腳本不必 hardcode 帳號）。"""
    result = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        capture_output=True, text=True, check=True,
        cwd=Path(__file__).parent,
    )
    url = result.stdout.strip()
    # 同時支援 https 跟 git@ 兩種 remote 格式
    m = re.search(r"github\.com[/:]([^/]+/[^/]+?)(\.git)?$", url)
    if not m:
        raise RuntimeError(f"Can't parse git remote URL: {url}")
    return m.group(1)


def fetch_comment_times(repo: str) -> list[datetime]:
    """從 GH 拉 Issue #1 所有 comment 的時間戳。"""
    result = subprocess.run(
        ["gh", "issue", "view", str(ISSUE_NUMBER), "--repo", repo,
         "--json", "comments"],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    return [
        datetime.fromisoformat(c["createdAt"].replace("Z", "+00:00"))
        for c in data["comments"]
    ]


def most_recent_completed_trading_day(now_utc: datetime):
    """回傳「最近一個已完成的交易日」UTC date。

    定義：UTC 是 Mon-Fri，且該日的最後一個排程時段 + GRACE 已經過了。
    範例：週六早上 07:00 Taipei 跑 → 對應 23:00 UTC 週六 → 最近完成的是
    週五（19:53 UTC + 60 min = 20:53 UTC，遠早於現在）。
    """
    d = now_utc.date()
    for _ in range(10):  # 找最近的，最多回看 10 天（防萬一）
        if d.weekday() < 5:  # Mon-Fri
            _, last_hr, last_mn = SCHEDULE_SLOTS_UTC[-1]
            close_deadline = datetime(
                d.year, d.month, d.day, last_hr, last_mn,
                tzinfo=timezone.utc,
            ) + timedelta(minutes=GRACE_MINUTES)
            if now_utc > close_deadline:
                return d
        d -= timedelta(days=1)
    return None


def missing_slots(target_day, comment_times: list[datetime], now_utc: datetime):
    """回傳該交易日漏掉的時段名稱列表。

    對每個排程時段，看是否有 comment 落在「主排程 - 5min」到「主 + grace」的窗口內。
    只檢查 deadline 已過的時段（避免「現在還早，當然沒 comment」的誤報）。
    """
    missing = []
    for name, hr, mn in SCHEDULE_SLOTS_UTC:
        scheduled = datetime(
            target_day.year, target_day.month, target_day.day, hr, mn,
            tzinfo=timezone.utc,
        )
        window_start = scheduled - timedelta(minutes=5)
        window_end = scheduled + timedelta(minutes=GRACE_MINUTES)
        if now_utc < window_end:
            continue  # 還沒到 deadline，跳過
        found = any(window_start <= ct <= window_end for ct in comment_times)
        if not found:
            missing.append((name, scheduled))
    return missing


def notify(title: str, message: str) -> None:
    """跳 macOS 原生通知。

    第一次跑會跳「允許通知？」的系統視窗——必須允許，否則之後都不會顯示。
    """
    # 引號 escape 避免 osascript 語法被破壞
    safe_title = title.replace('"', '\\"')
    safe_msg = message.replace('"', '\\"')
    subprocess.run(
        ["osascript", "-e",
         f'display notification "{safe_msg}" with title "{safe_title}"'],
        check=False,  # 通知失敗不要讓整個腳本壞掉
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--test", action="store_true",
                    help="強制送一個通知測試 macOS 權限")
    args = ap.parse_args(argv)

    if args.test:
        notify("trade_bot monitor", "測試通知 — 設定正常")
        print("發了測試通知。沒看到通知？檢查 System Settings → Notifications。")
        return 0

    now = datetime.now(timezone.utc)
    target_day = most_recent_completed_trading_day(now)
    if target_day is None:
        # 理論上找得到 10 天內的工作日；極罕見的邊界情況
        print(f"[{now.isoformat()}] 找不到最近的已完成交易日。")
        return 0

    repo = get_repo_slug()
    comment_times = fetch_comment_times(repo)
    missing = missing_slots(target_day, comment_times, now)

    if missing:
        slots_str = ", ".join(
            f"{name} ({s.strftime('%H:%M')} UTC)" for name, s in missing
        )
        msg = f"{target_day.isoformat()} 漏跑: {slots_str}"
        print(f"[{now.isoformat()}] {msg}")
        notify("trade_bot 漏跑了", msg)
    else:
        print(f"[{now.isoformat()}] {target_day.isoformat()} 三個時段都有 comment ✓")

    return 0


if __name__ == "__main__":
    sys.exit(main())

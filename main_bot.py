"""trade_bot 主程式 — 連上 Robinhood、跑一次再平衡。

預設是 review-only（不下單）；加 --execute 才會真的呼叫 place_equity_order。

完整流程：
  1. 從 .env 讀 RH_AGENTIC_ACCOUNT（你的 sub-account 編號）
  2. 用 rh_session() 拿到已登入的 MCP session
  3. 安全檢查：查 get_equity_orders → 如有 TQQQ/SGOV 的「未成交」單，整輪跳過
     （避免 cron 連續觸發時下重複單）
  4. 抓 get_equity_positions / get_equity_quotes / get_portfolio
  5. 餵 rebalance.decide() 得到抽象計畫
  6. build_concrete_orders() 轉成整數股 + marketable limit 的具體訂單
  7. 對每張單呼叫 review_equity_order（broker 端的乾跑檢查）
  8. 視旗標決定接下來：
       --execute   且 review 沒警示 → 呼叫 place_equity_order 真的送單
       --execute   且 review 有警示 → 拒絕送這一張單（印警示，繼續下一張）
       不加旗標     → 只印 review 結果，不送任何單

★ 安全保證 ★
  - place_equity_order 只在 --execute 顯式給定時才會被呼叫
  - 任何 review 警示都會中斷該單的送出
  - 已存在的開放單會讓整輪跳過（防止重複下單）
  - --smoke-review 跟 --execute 互斥（避免送出 $1 假單）

使用方式：
    uv run main_bot.py                  # review-only（預設）
    uv run main_bot.py --smoke-review   # 帳戶錢少時，用假單測試 review API
    uv run main_bot.py --execute        # 真的下單（不可逆，慎用）
    uv run main_bot.py --account NNNN   # 覆寫 .env 裡的帳號
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP

from dotenv import load_dotenv

from mcp_client import parse_tool_json, rh_session
from rebalance import (
    SGOV, TQQQ, Decision, decide,
    parse_cash, parse_positions, parse_quotes, render,
)

# 限價偏移幅度
# 賣單限價 = bid_price × (1 − 0.005)  → 比買方願意付的價低 0.5%
# 買單限價 = ask_price × (1 + 0.005)  → 比賣方願意收的價高 0.5%
# 「marketable limit」設計：跨過對方的價，幾乎一定立刻成交，
# 但限價提供一個「最差成交價」的保護，避免極端 spread 時被坑
# 0.5% 對 TQQQ/SGOV 這兩支流動性好的標的是寬的（spread 通常 < 0.1%）
LIMIT_SLIP = Decimal("0.005")

# Robinhood 訂單 state 屬於「未成交、還在 order book 裡」的狀態
# 跑前如有任何一張這類單存在於 TQQQ 或 SGOV → 整輪跳過避免重複下單
OPEN_ORDER_STATES = {"new", "queued", "confirmed", "unconfirmed", "partially_filled"}


@dataclass
class ConcreteOrder:
    """可以真的送給 broker 的訂單。"""
    side: str               # "buy" or "sell"
    symbol: str
    quantity: int           # 整數股（fractional 對 limit 單不允許）
    limit_price: Decimal    # 已 round 到分位
    notional: Decimal       # quantity × limit_price，純資訊（顯示用）


def _floor_cents(x: Decimal) -> Decimal:
    """取到分位、無條件捨去。賣單限價往下取（更願意賣 → 更容易成交）。"""
    return x.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def _ceil_cents(x: Decimal) -> Decimal:
    """取到分位、無條件進位。買單限價往上取（更願意付 → 更容易成交）。"""
    return x.quantize(Decimal("0.01"), rounding=ROUND_UP)


def build_concrete_orders(d: Decision, quotes) -> list[ConcreteOrder]:
    """把 rebalance.decide() 的抽象 orders 轉成可送的整數股限價單。

    賣單：取整數股（floor），限價 = floor_cents(bid × 0.995)
    買單：限價 = ceil_cents(ask × 1.005)，股數 = floor(預算 / 限價)
    股數 round 為 0 → 跳過。bid/ask 為 0（盤外）→ 退用 last_trade_price。
    """
    out: list[ConcreteOrder] = []
    for o in d.orders:
        q = quotes[o.symbol]
        if o.side == "sell":
            assert o.quantity is not None
            ref = q.bid_price if q.bid_price > 0 else q.last_trade_price
            limit = _floor_cents(ref * (Decimal("1") - LIMIT_SLIP))
            shares = int(o.quantity)
            if shares <= 0 or limit <= 0:
                continue
            out.append(ConcreteOrder("sell", o.symbol, shares, limit, shares * limit))
        else:
            assert o.dollars is not None
            ref = q.ask_price if q.ask_price > 0 else q.last_trade_price
            limit = _ceil_cents(ref * (Decimal("1") + LIMIT_SLIP))
            if limit <= 0:
                continue
            shares = int(o.dollars // limit)
            if shares <= 0:
                continue
            out.append(ConcreteOrder("buy", o.symbol, shares, limit, shares * limit))
    return out


def render_concrete(orders: list[ConcreteOrder]) -> str:
    """把具體訂單列表轉成易讀的多行字串。"""
    if not orders:
        return "No actionable whole-share orders (deltas too small to clear 1 share)."
    lines = ["Concrete orders (whole shares, marketable limits):"]
    for o in orders:
        verb = "SELL" if o.side == "sell" else "BUY "
        lines.append(
            f"  {verb} {o.quantity:>5} {o.symbol} @ ${o.limit_price} limit"
            f"  (~${o.notional:,.2f})"
        )
    return "\n".join(lines)


def render_review_alerts(payload: dict) -> tuple[bool, str]:
    """把 review_equity_order 回傳的 order_checks 解析成 (有警示嗎, 顯示字串)。"""
    checks = payload.get("data", {}).get("order_checks", {})
    if not checks:
        return False, "    [ok] no alerts"
    return True, "    [alert] " + json.dumps(checks, ensure_ascii=False)


async def check_open_orders(session, account: str) -> list[dict]:
    """查帳戶內 TQQQ/SGOV 的未成交單。回傳 list；空 list 表示安全可下單。"""
    res = await session.call_tool("get_equity_orders", {"account_number": account})
    payload = parse_tool_json(res)
    return [
        o for o in payload.get("data", {}).get("orders", [])
        if o.get("state") in OPEN_ORDER_STATES and o.get("symbol") in (TQQQ, SGOV)
    ]


async def run(account: str, smoke_review: bool, execute: bool) -> int:
    """一次完整的再平衡流程。回傳值給 sys.exit 用（0 = 成功）。"""
    async with rh_session() as session:
        # ── 安全檢查 1：先看有沒有未成交單 ──
        # 不論是不是 --execute，都先檢查；有的話我們連 review 都不必跑
        open_orders = await check_open_orders(session, account)
        if open_orders:
            print("Skipping this cycle — open orders exist on TQQQ/SGOV:")
            for o in open_orders:
                print(f"  {o.get('state')}  {o.get('side')} {o.get('quantity')} "
                      f"{o.get('symbol')}  (id {o.get('id')})")
            print("\n等這些單成交或取消後再跑。")
            return 0

        # ── 抓帳戶現況（三個都是 read-only）──
        positions_raw = parse_tool_json(await session.call_tool(
            "get_equity_positions", {"account_number": account}))
        quotes_raw = parse_tool_json(await session.call_tool(
            "get_equity_quotes", {"symbols": [TQQQ, SGOV]}))
        portfolio_raw = parse_tool_json(await session.call_tool(
            "get_portfolio", {"account_number": account}))

        positions = parse_positions(positions_raw)
        quotes = parse_quotes(quotes_raw)
        cash = parse_cash(portfolio_raw)

        # ── 跑策略邏輯 ──
        decision = decide(positions, quotes, cash)
        print(render(decision))
        print()

        # ── 轉成具體訂單 ──
        concrete = build_concrete_orders(decision, quotes)
        print(render_concrete(concrete))

        # smoke-review 路徑：合成不會成交的假單，純為了測 review API 串接
        # main() 已禁止跟 --execute 同時開，這裡不會跟下單路徑混
        if smoke_review and not concrete:
            print("\n[--smoke-review] Constructing a no-fill BUY 1 SGOV @ $1.00 to "
                  "verify review_equity_order plumbing.")
            concrete = [ConcreteOrder("buy", SGOV, 1, Decimal("1.00"), Decimal("1.00"))]

        if not concrete:
            return 0

        # ── 對每張訂單 review → 視 --execute 決定要不要 place ──
        if execute:
            print("\n*** --execute MODE — real orders will be placed ***\n")
        else:
            print("\nReview results (no orders placed):")

        for o in concrete:
            review_res = await session.call_tool("review_equity_order", {
                "account_number": account,
                "symbol": o.symbol,
                "side": o.side,
                "type": "limit",
                "quantity": str(o.quantity),
                "limit_price": str(o.limit_price),
                "time_in_force": "gfd",
                "market_hours": "regular_hours",
            })
            review_payload = parse_tool_json(review_res)
            has_alerts, alert_str = render_review_alerts(review_payload)
            verb = "SELL" if o.side == "sell" else "BUY "
            print(f"  {verb} {o.quantity} {o.symbol} @ ${o.limit_price}:")
            print(alert_str)

            # ── 進入下單路徑 ──
            if not execute:
                continue  # review-only：印完就走

            if has_alerts:
                # broker 認為這單有問題（買力不足、PDT、停牌等）→ 不送
                print(f"    >>> REFUSING to place: review returned alerts.")
                continue

            # 安全防呆：smoke-review 的假單即使在 --execute 模式也不送
            # （main() 已禁止這組合，但雙重防線）
            if smoke_review:
                print(f"    >>> SKIPPING smoke-review synthetic order in --execute mode.")
                continue

            # ── 真的送單 ──
            # ref_id 是給 broker 的 idempotency key：同一 ref_id 重送會被去重
            # 每張單一個新 UUID；這次跑失敗就失敗，下一輪 cron 會重算
            ref_id = str(uuid.uuid4())
            place_res = await session.call_tool("place_equity_order", {
                "account_number": account,
                "symbol": o.symbol,
                "side": o.side,
                "type": "limit",
                "quantity": str(o.quantity),
                "limit_price": str(o.limit_price),
                "time_in_force": "gfd",
                "market_hours": "regular_hours",
                "ref_id": ref_id,
            })
            place_payload = parse_tool_json(place_res)
            data = place_payload.get("data", {})
            print(f"    >>> PLACED  order_id={data.get('id')}  state={data.get('state')}")

        return 0


def main() -> int:
    """CLI 入口：解析旗標、檢查環境變數、跑主流程。"""
    load_dotenv()  # 把 .env 裡的變數塞進 os.environ

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account", default=None,
                    help="Agentic 帳號（覆寫 RH_AGENTIC_ACCOUNT 環境變數）")

    # --smoke-review 跟 --execute 互斥
    # 原因：smoke-review 會合成 $1 假單；跟 --execute 一起會變成真的送 $1 假單
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--smoke-review", action="store_true",
                    help="無實際可下單時，合成假單測試 review API")
    grp.add_argument("--execute", action="store_true",
                    help="真實下單。review 無警示才會送（不可逆，慎用）")
    args = ap.parse_args()

    # 帳號優先序：--account > 環境變數 > 直接失敗
    account = args.account or os.environ.get("RH_AGENTIC_ACCOUNT")
    if not account:
        print("error: 找不到帳號。請設定 RH_AGENTIC_ACCOUNT 環境變數（或複製 "
              ".env.example 成 .env 並填入），或用 --account NNNN 指定。",
              file=sys.stderr)
        return 2

    try:
        return asyncio.run(run(account, args.smoke_review, args.execute))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

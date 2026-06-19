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

# ─── 下單方式切換 ──────────────────────────────────────────
# "market" → 市價單；允許 fractional 股；對 TQQQ/SGOV 這種高流動性 ETF
#            在 regular hours 內 spread 通常 < 0.1%，滑價極小。
#            買用 dollar_amount（精準到分），賣用 quantity（精準到 6 位小數）。
# "limit"  → 限價單；只能整數股；提供「最差成交價」保護。
#            < 1 股的訂單會被跳過（無法部署小金額）。
# 兩條路徑都在程式碼裡，改這個常數就切換，沒有副作用。
ORDER_TYPE = "market"

# 限價偏移幅度（只在 ORDER_TYPE = "limit" 時用到）
# 賣單限價 = bid_price × (1 − 0.005)  → 比買方願意付的價低 0.5%
# 買單限價 = ask_price × (1 + 0.005)  → 比賣方願意收的價高 0.5%
# 0.5% 對 TQQQ/SGOV 這兩支流動性好的標的是寬的（spread 通常 < 0.1%）
LIMIT_SLIP = Decimal("0.005")

# Robinhood 訂單 state 屬於「未成交、還在 order book 裡」的狀態
# 跑前如有任何一張這類單存在於 TQQQ 或 SGOV → 整輪跳過避免重複下單
OPEN_ORDER_STATES = {"new", "queued", "confirmed", "unconfirmed", "partially_filled"}


@dataclass
class ConcreteOrder:
    """可以真的送給 broker 的訂單。

    review_params: 已準備好直接餵給 review_equity_order / place_equity_order
                   的參數 dict（不含 account_number 和 ref_id）。
    description:   人讀的描述，例如 "BUY $5.00 of TQQQ @ market"
    """
    side: str
    symbol: str
    description: str
    review_params: dict[str, str]


def _floor_cents(x: Decimal) -> Decimal:
    """取到分位、無條件捨去。賣單限價往下取（更願意賣 → 更容易成交）。"""
    return x.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def _ceil_cents(x: Decimal) -> Decimal:
    """取到分位、無條件進位。買單限價往上取（更願意付 → 更容易成交）。"""
    return x.quantize(Decimal("0.01"), rounding=ROUND_UP)


def _build_market(o, q) -> tuple[dict, str] | None:
    """市價單路徑：fractional 可用，無價格保護。回 None 代表跳過。"""
    common = {
        "symbol": o.symbol,
        "type": "market",
        "market_hours": "regular_hours",  # fractional 規定要 regular hours
    }
    if o.side == "sell":
        assert o.quantity is not None
        # 賣股數：quantize 到小數點 6 位、無條件捨去（避免賣超）
        qty = o.quantity.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        if qty <= 0:
            return None
        # 估算成交金額用 bid 推（純顯示用）
        ref = q.bid_price if q.bid_price > 0 else q.last_trade_price
        est = qty * ref
        return (
            {**common, "side": "sell", "quantity": str(qty)},
            f"SELL {qty} {o.symbol} @ market  (~${est:,.2f})",
        )
    else:
        assert o.dollars is not None
        # 買美金：quantize 到分、無條件捨去（避免買超預算）
        usd = o.dollars.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if usd <= 0:
            return None
        return (
            {**common, "side": "buy", "dollar_amount": str(usd)},
            f"BUY  ${usd} of {o.symbol} @ market",
        )


def _build_limit(o, q) -> tuple[dict, str] | None:
    """限價單路徑：只能整數股，有「最差成交價」保護。回 None 代表跳過。"""
    common = {
        "symbol": o.symbol,
        "type": "limit",
        "time_in_force": "gfd",
        "market_hours": "regular_hours",
    }
    if o.side == "sell":
        assert o.quantity is not None
        ref = q.bid_price if q.bid_price > 0 else q.last_trade_price
        limit = _floor_cents(ref * (Decimal("1") - LIMIT_SLIP))
        shares = int(o.quantity)  # 整數股 round down
        if shares <= 0 or limit <= 0:
            return None
        return (
            {**common, "side": "sell", "quantity": str(shares),
             "limit_price": str(limit)},
            f"SELL {shares:>5} {o.symbol} @ ${limit} limit  (~${shares * limit:,.2f})",
        )
    else:
        assert o.dollars is not None
        ref = q.ask_price if q.ask_price > 0 else q.last_trade_price
        limit = _ceil_cents(ref * (Decimal("1") + LIMIT_SLIP))
        if limit <= 0:
            return None
        shares = int(o.dollars // limit)
        if shares <= 0:
            return None
        return (
            {**common, "side": "buy", "quantity": str(shares),
             "limit_price": str(limit)},
            f"BUY  {shares:>5} {o.symbol} @ ${limit} limit  (~${shares * limit:,.2f})",
        )


def build_concrete_orders(d: Decision, quotes) -> list[ConcreteOrder]:
    """把 rebalance.decide() 的抽象 orders 轉成可送的 broker 訂單。

    走哪條路徑由 ORDER_TYPE 決定（"market" 或 "limit"）。
    回傳列表裡可能少於 d.orders（被 builder 跳過的不會進去）。
    """
    if ORDER_TYPE not in ("market", "limit"):
        raise ValueError(f"Unknown ORDER_TYPE: {ORDER_TYPE}")
    builder = _build_market if ORDER_TYPE == "market" else _build_limit

    out: list[ConcreteOrder] = []
    for o in d.orders:
        result = builder(o, quotes[o.symbol])
        if result is None:
            continue
        params, desc = result
        out.append(ConcreteOrder(o.side, o.symbol, desc, params))
    return out


def render_concrete(orders: list[ConcreteOrder]) -> str:
    """把具體訂單列表轉成易讀的多行字串。"""
    if not orders:
        return f"No actionable orders (ORDER_TYPE={ORDER_TYPE})."
    lines = [f"Concrete orders (ORDER_TYPE={ORDER_TYPE}):"]
    for o in orders:
        lines.append(f"  {o.description}")
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
        # 注意：smoke-review 一律建限價假單，跟 ORDER_TYPE 設定無關
        # 因為市價單一定會成交，不能用來測「review-only 不會下單」這件事
        # main() 已禁止 smoke-review 跟 --execute 同時開
        if smoke_review and not concrete:
            print("\n[--smoke-review] Constructing a no-fill BUY 1 SGOV @ $1.00 to "
                  "verify review_equity_order plumbing.")
            concrete = [ConcreteOrder(
                side="buy",
                symbol=SGOV,
                description="BUY 1 SGOV @ $1.00 limit (smoke test, would not fill)",
                review_params={
                    "symbol": SGOV, "side": "buy", "type": "limit",
                    "quantity": "1", "limit_price": "1.00",
                    "time_in_force": "gfd", "market_hours": "regular_hours",
                },
            )]

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
                **o.review_params,
            })
            review_payload = parse_tool_json(review_res)
            has_alerts, alert_str = render_review_alerts(review_payload)
            print(f"  {o.description}:")
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
                **o.review_params,
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

"""一次性清倉腳本 — 賣掉指定 symbol 的全部持倉。

用法：
    uv run liquidate.py SYMBOL [SYMBOL ...]
    # 例：uv run liquidate.py SOXL SGOV

行為：
  對每個 symbol，先 review_equity_order 看有無 alert，
  沒 alert 才 place_equity_order 賣掉「目前持倉的全部股數」。

跟 main_bot.py 的差別：這支腳本不認策略、不算 ratio、不查 open orders，
就是「賣指定股票全部持倉」。給策略轉換時的一次性清倉用。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

from dotenv import load_dotenv

from mcp_client import parse_tool_json, rh_session


async def main(symbols: list[str]) -> int:
    load_dotenv()
    account = os.environ.get("RH_AGENTIC_ACCOUNT")
    if not account:
        print("error: RH_AGENTIC_ACCOUNT not set", file=sys.stderr)
        return 2

    async with rh_session() as s:
        # 抓持倉
        pos = parse_tool_json(await s.call_tool(
            "get_equity_positions", {"account_number": account}))
        sym_qty = {p["symbol"]: p["quantity"] for p in pos["data"]["positions"]}

        for sym in symbols:
            qty = sym_qty.get(sym)
            print(f"\n=== {sym} ===")
            if not qty or float(qty) <= 0:
                print(f"  無持倉，跳過")
                continue

            print(f"  持倉 {qty} 股，賣全部")
            # Review 先確認無 alert
            review = parse_tool_json(await s.call_tool("review_equity_order", {
                "account_number": account, "symbol": sym, "side": "sell",
                "type": "market", "quantity": qty,
                "market_hours": "regular_hours",
            }))
            alerts = review["data"].get("order_checks", {})
            if alerts:
                print(f"  REFUSING — review alerts: {json.dumps(alerts)}")
                continue

            print(f"  review OK，送出市價單...")
            res = parse_tool_json(await s.call_tool("place_equity_order", {
                "account_number": account, "symbol": sym, "side": "sell",
                "type": "market", "quantity": qty,
                "market_hours": "regular_hours",
                "ref_id": str(uuid.uuid4()),
            }))
            data = res.get("data", {})
            print(f"  PLACED: state={data.get('state')} id={data.get('id')}")

    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    sys.exit(asyncio.run(main(sys.argv[1:])))

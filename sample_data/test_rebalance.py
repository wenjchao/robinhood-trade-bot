"""rebalance.decide() 的單元測試。

執行方式（必須從專案根目錄 trade_bot/ 跑，因為要 import 到上一層的 rebalance.py）：
    uv run python -m unittest sample_data.test_rebalance -v

或直接執行此檔：
    uv run python sample_data/test_rebalance.py
"""

from __future__ import annotations

import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path

# 把專案根目錄（這個檔案的上一層）加進 sys.path，這樣才能 import rebalance.py
# 為什麼需要：這個檔案搬到 sample_data/ 之後，預設的 import 路徑找不到 rebalance.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rebalance import SGOV, TQQQ, decide, parse_cash, parse_positions, parse_quotes  # noqa: E402

# 樣本資料就在這個檔案的同一個目錄
SAMPLES = Path(__file__).parent


def _load(positions_file: str, portfolio_file: str = "portfolio_no_cash.json",
          quotes_file: str = "quotes_live.json"):
    """讀三個 JSON 檔、跑 decide()、回傳 Decision。"""
    positions = parse_positions(json.loads((SAMPLES / positions_file).read_text()))
    quotes = parse_quotes(json.loads((SAMPLES / quotes_file).read_text()))
    cash = parse_cash(json.loads((SAMPLES / portfolio_file).read_text()))
    return decide(positions, quotes, cash)


def _orders_by_side(d):
    """把 orders list 變成 {(side, symbol): Order} 方便測試查找。"""
    return {(o.side, o.symbol): o for o in d.orders}


class TestDecide(unittest.TestCase):
    def test_empty_account_no_cash_is_idle(self):
        """完全空帳戶 → idle（什麼都不做）。"""
        d = _load("positions_empty.json", "portfolio_no_cash.json")
        self.assertEqual(d.action, "idle")
        self.assertEqual(d.orders, [])

    def test_empty_account_with_cash_deploys_half_half(self):
        """有現金、沒持倉 → 把現金平分買兩腿。"""
        # $10000 cash, no positions: each leg target = $5000
        d = _load("positions_empty.json", "portfolio_10k_cash.json")
        self.assertEqual(d.action, "rebalance")
        self.assertEqual(d.target_per_leg, Decimal("5000"))
        orders = _orders_by_side(d)
        self.assertEqual(orders[("buy", TQQQ)].dollars, Decimal("5000"))
        self.assertEqual(orders[("buy", SGOV)].dollars, Decimal("5000"))
        self.assertNotIn(("sell", TQQQ), orders)
        self.assertNotIn(("sell", SGOV), orders)

    def test_in_band_no_cash_holds(self):
        """比例在帶內、沒現金 → hold。"""
        d = _load("positions_in_band.json", "portfolio_no_cash.json")
        self.assertEqual(d.action, "hold")
        self.assertEqual(d.orders, [])

    def test_in_band_with_cash_still_holds(self):
        """比例在帶內、有現金 → 仍 hold（按嚴格規則，閒置現金不自動部署）。"""
        d = _load("positions_in_band.json", "portfolio_10k_cash.json")
        self.assertEqual(d.action, "hold")
        self.assertIn("idle", d.reason.lower())

    def test_tqqq_high_no_cash_sells_tqqq_buys_sgov(self):
        """TQQQ 過重、沒現金 → 賣 TQQQ、買 SGOV。"""
        # 110*82.92=9121.2, 80*100.59=8047.2, ratio 1.1335 > 1.05
        # target = (9121.2+8047.2)/2 = 8584.2
        d = _load("positions_tqqq_high.json", "portfolio_no_cash.json")
        self.assertEqual(d.action, "rebalance")
        orders = _orders_by_side(d)
        self.assertAlmostEqual(float(orders[("sell", TQQQ)].quantity),
                               537.0 / 82.92, places=3)
        self.assertAlmostEqual(float(orders[("buy", SGOV)].dollars), 537.0, places=1)
        self.assertNotIn(("sell", SGOV), orders)

    def test_sgov_high_with_cash_buys_more_tqqq(self):
        """SGOV 過重、有 $10k 現金 → 同時買兩腿，不用賣。

        80*82.92=6633.6, 90*100.59=9053.1, cash=10000
        total=25686.7, target=12843.35
        兩腿都低於 target → 全用現金買，不需賣任何一邊
        """
        d = _load("positions_sgov_high.json", "portfolio_10k_cash.json")
        self.assertEqual(d.action, "rebalance")
        orders = _orders_by_side(d)
        self.assertAlmostEqual(float(orders[("buy", TQQQ)].dollars), 6209.75, places=1)
        self.assertAlmostEqual(float(orders[("buy", SGOV)].dollars), 3790.25, places=1)
        self.assertNotIn(("sell", TQQQ), orders)
        self.assertNotIn(("sell", SGOV), orders)


if __name__ == "__main__":
    unittest.main()

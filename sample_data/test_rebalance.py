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
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rebalance import (  # noqa: E402
    TQQQ, VBIL, decide, parse_cash, parse_positions, parse_quotes,
)

SAMPLES = Path(__file__).parent

# 樣本 quotes_live.json 裡的 last_trade_price（用來算測試的預期值）
P_TQQQ = 82.62
P_VBIL = 75.63


def _load(positions_file: str, portfolio_file: str = "portfolio_no_cash.json",
          quotes_file: str = "quotes_live.json"):
    positions = parse_positions(json.loads((SAMPLES / positions_file).read_text()))
    quotes = parse_quotes(json.loads((SAMPLES / quotes_file).read_text()))
    cash = parse_cash(json.loads((SAMPLES / portfolio_file).read_text()))
    return decide(positions, quotes, cash)


def _orders_by_side(d):
    return {(o.side, o.symbol): o for o in d.orders}


class TestDecide(unittest.TestCase):
    def test_empty_account_no_cash_is_idle(self):
        """完全空帳戶 → idle（什麼都不做）。"""
        d = _load("positions_empty.json", "portfolio_no_cash.json")
        self.assertEqual(d.action, "idle")
        self.assertEqual(d.orders, [])

    def test_empty_account_with_cash_deploys_2_to_1(self):
        """有 $10k 現金、沒持倉 → 買 2/3 TQQQ + 1/3 VBIL。"""
        d = _load("positions_empty.json", "portfolio_10k_cash.json")
        self.assertEqual(d.action, "rebalance")
        # target_tqqq = 10000 × 2/3 ≈ 6666.67
        # target_vbil = 10000 × 1/3 ≈ 3333.33
        self.assertAlmostEqual(float(d.target_tqqq), 6666.667, places=2)
        self.assertAlmostEqual(float(d.target_vbil), 3333.333, places=2)
        orders = _orders_by_side(d)
        self.assertAlmostEqual(float(orders[("buy", TQQQ)].dollars), 6666.667, places=2)
        self.assertAlmostEqual(float(orders[("buy", VBIL)].dollars), 3333.333, places=2)
        self.assertNotIn(("sell", TQQQ), orders)
        self.assertNotIn(("sell", VBIL), orders)

    def test_in_band_no_cash_holds(self):
        """比例在 [1.905, 2.10] 帶內、沒現金 → hold。

        200 TQQQ × 82.62 = 16524, 109 VBIL × 75.63 = 8243.67
        ratio = 16524 / 8243.67 ≈ 2.004 → in band ✓
        """
        d = _load("positions_in_band.json", "portfolio_no_cash.json")
        self.assertEqual(d.action, "hold")
        self.assertGreater(d.ratio, Decimal("1.905"))
        self.assertLess(d.ratio, Decimal("2.10"))
        self.assertEqual(d.orders, [])

    def test_in_band_with_cash_still_holds(self):
        """比例在帶內、有現金 → 仍 hold（嚴格規則：閒置現金不自動部署）。"""
        d = _load("positions_in_band.json", "portfolio_10k_cash.json")
        self.assertEqual(d.action, "hold")
        self.assertIn("idle", d.reason.lower())

    def test_tqqq_high_no_cash_sells_tqqq_buys_vbil(self):
        """TQQQ 過重（ratio > 2.10）、沒現金 → 賣 TQQQ、買 VBIL。

        250 TQQQ × 82.62 = 20655, 100 VBIL × 75.63 = 7563
        ratio = 2.731 → out of band
        total = 28218, target_tqqq = 18812, target_vbil = 9406
        sell TQQQ: 20655 - 18812 = 1843 → qty = 1843/82.62 ≈ 22.31
        buy VBIL: 9406 - 7563 = 1843
        """
        d = _load("positions_tqqq_high.json", "portfolio_no_cash.json")
        self.assertEqual(d.action, "rebalance")
        self.assertGreater(d.ratio, Decimal("2.10"))
        orders = _orders_by_side(d)
        self.assertAlmostEqual(float(orders[("sell", TQQQ)].quantity),
                               1843.0 / P_TQQQ, places=2)
        self.assertAlmostEqual(float(orders[("buy", VBIL)].dollars), 1843.0, places=1)
        self.assertNotIn(("sell", VBIL), orders)

    def test_vbil_high_with_cash_buys_both_no_sell(self):
        """VBIL 過重（ratio < 1.905）但帳上有 $10k 現金 → 兩腿都買，不用賣。

        100 TQQQ × 82.62 = 8262, 100 VBIL × 75.63 = 7563, cash = 10000
        ratio = 8262 / 7563 ≈ 1.092 → out of band ✓
        total = 25825, target_tqqq ≈ 17216.67, target_vbil ≈ 8608.33
        buy TQQQ: 17216.67 - 8262 ≈ 8954.67
        buy VBIL: 8608.33 - 7563 ≈ 1045.33
        sum ≈ 10000 = cash ✓
        """
        d = _load("positions_vbil_high.json", "portfolio_10k_cash.json")
        self.assertEqual(d.action, "rebalance")
        self.assertLess(d.ratio, Decimal("1.905"))
        orders = _orders_by_side(d)
        self.assertAlmostEqual(float(orders[("buy", TQQQ)].dollars), 8954.667, places=2)
        self.assertAlmostEqual(float(orders[("buy", VBIL)].dollars), 1045.333, places=2)
        self.assertNotIn(("sell", TQQQ), orders)
        self.assertNotIn(("sell", VBIL), orders)


if __name__ == "__main__":
    unittest.main()
